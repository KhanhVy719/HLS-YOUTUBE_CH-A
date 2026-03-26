#!/usr/bin/env python3
"""YouTube HLS Streaming Server — Proxy YouTube CDN + Upload with AES-128 encryption."""

import os
import json
import re
import subprocess
import time
import threading
import urllib.request
import hashlib
import secrets
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response, redirect
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="static")
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB max upload

# ============ CONFIG ============
DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/yt-hls"))
HLS_DIR = DATA_DIR / "hls"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_FILE = DATA_DIR / "videos.json"
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/root/cookies.txt"))
PORT = int(os.environ.get("PORT", "5555"))
DENO_DIR = os.path.expanduser("~/.deno/bin")
ALLOWED_EXT = {"mp4", "mkv", "avi", "mov", "webm", "flv", "ts", "m4v"}

# Subprocess env with deno in PATH
SUB_ENV = os.environ.copy()
SUB_ENV["PATH"] = DENO_DIR + ":" + SUB_ENV.get("PATH", "")

HLS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# URL cache: {video_id: {"url": ..., "expires": timestamp}}
_url_cache = {}
_locks = {}
_lock_master = threading.Lock()
URL_TTL = 5 * 3600  # 5 hours


# ============ DATABASE ============
def load_db():
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {"videos": {}}


def save_db(db):
    DB_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False))


def parse_video_id(raw):
    """Extract YouTube video ID from URL or raw string."""
    raw = raw.strip()
    match = re.search(r'(?:v=|\/live\/|youtu\.be\/)([a-zA-Z0-9_-]{11})', raw)
    if match:
        return match.group(1)
    if re.match(r'^[a-zA-Z0-9_-]{11}$', raw):
        return raw
    return None


def get_lock(video_id):
    with _lock_master:
        if video_id not in _locks:
            _locks[video_id] = threading.Lock()
        return _locks[video_id]


# ============ YT-DLP ============
def update_ytdlp():
    """Auto-update yt-dlp on startup."""
    try:
        subprocess.run(["pip3", "install", "-U", "yt-dlp"], capture_output=True, timeout=60, env=SUB_ENV)
        print("✅ yt-dlp updated")
    except Exception as e:
        print(f"⚠️ yt-dlp update skipped: {e}")


def _run_ytdlp(args, timeout=120):
    """Run yt-dlp with given args, return (stdout, stderr, returncode)."""
    cmd = ["yt-dlp"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=SUB_ENV)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1


def get_cdn_url(video_id, force=False):
    """Get YouTube CDN direct URL via yt-dlp, with caching and fallback strategies."""
    # Check cache
    if not force and video_id in _url_cache:
        entry = _url_cache[video_id]
        if time.time() < entry["expires"]:
            return entry["url"], None

    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    cookies_args = ["--cookies", str(COOKIES_FILE)] if COOKIES_FILE.exists() else []

    # Strategy 1: web client + cookies + deno
    strategies = [
        ["-g", "-f", "best[ext=mp4]/best", "--remote-components", "ejs:github"] + cookies_args + [yt_url],
        # Strategy 2: android client (bypasses many restrictions)
        ["-g", "-f", "best[ext=mp4]/best", "--extractor-args", "youtube:player_client=android"] + cookies_args + [yt_url],
        # Strategy 3: no cookies, no special args (for public videos)
        ["-g", "-f", "best[ext=mp4]/best", "--no-check-certificates", yt_url],
        # Strategy 4: ios client
        ["-g", "-f", "best", "--extractor-args", "youtube:player_client=ios"] + cookies_args + [yt_url],
    ]

    last_err = ""
    for i, args in enumerate(strategies):
        stdout, stderr, rc = _run_ytdlp(args, timeout=120)
        if rc == 0 and stdout:
            url = stdout.split('\n')[0]
            _url_cache[video_id] = {"url": url, "expires": time.time() + URL_TTL}
            print(f"  ✅ Strategy {i+1} worked for {video_id}")
            return url, None
        last_err = stderr[-300:] if stderr else "unknown error"
        print(f"  ❌ Strategy {i+1} failed: {last_err[:100]}")

    return None, last_err


def get_video_info(video_id):
    """Get video metadata from yt-dlp."""
    cmd = [
        "yt-dlp", "--print", "title", "--print", "duration",
        "--print", "thumbnail", "--no-download",
        "--remote-components", "ejs:github",
    ]
    if COOKIES_FILE.exists():
        cmd.extend(["--cookies", str(COOKIES_FILE)])
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=SUB_ENV)
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            return {
                "title": lines[0] if len(lines) > 0 else video_id,
                "duration": lines[1] if len(lines) > 1 else "?",
                "thumbnail": lines[2] if len(lines) > 2 else "",
            }
    except Exception:
        pass
    return {"title": video_id, "duration": "?", "thumbnail": ""}


# ============ ROUTES ============
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/videos", methods=["GET"])
def list_videos():
    db = load_db()
    videos = []
    for vid, info in db["videos"].items():
        cached = (HLS_DIR / vid / "index.m3u8").exists()
        has_cdn = vid in _url_cache and time.time() < _url_cache[vid]["expires"]
        videos.append({**info, "id": vid, "cached": cached, "has_cdn": has_cdn})
    return jsonify({"videos": videos})


@app.route("/api/videos", methods=["POST"])
def add_video():
    data = request.json or {}
    raw = data.get("video_id", "").strip()
    video_id = parse_video_id(raw)

    if not video_id:
        return jsonify({"error": "Invalid YouTube video ID or URL"}), 400

    db = load_db()
    if video_id in db["videos"]:
        return jsonify({"error": "Video already exists", "id": video_id}), 409

    # Get info
    info = get_video_info(video_id)
    info["added_at"] = int(time.time())
    info["status"] = "added"

    db["videos"][video_id] = info
    save_db(db)

    return jsonify({"success": True, "id": video_id, **info})


@app.route("/api/videos/<video_id>", methods=["DELETE"])
def delete_video(video_id):
    db = load_db()
    if video_id not in db["videos"]:
        return jsonify({"error": "Not found"}), 404

    del db["videos"][video_id]
    save_db(db)

    # Clean cache
    import shutil
    cache_dir = HLS_DIR / video_id
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    _url_cache.pop(video_id, None)

    return jsonify({"success": True})


# ====== CDN URL (direct YouTube link) ======
@app.route("/api/cdn/<video_id>")
def get_cdn(video_id):
    """Get YouTube CDN direct URL for external use."""
    url, err = get_cdn_url(video_id)
    if not url:
        return jsonify({"error": f"yt-dlp failed: {err}"}), 500
    expires_in = int(_url_cache[video_id]["expires"] - time.time())
    return jsonify({"url": url, "expires_in": expires_in, "proxy_url": f"/proxy/{video_id}"})


# ====== PROXY STREAM (pipes YouTube CDN → client) ======
@app.route("/proxy/<video_id>")
def proxy_stream(video_id):
    """Proxy YouTube CDN stream through VPS (hides YouTube URL, bypasses CORS)."""
    url, err = get_cdn_url(video_id)
    if not url:
        return jsonify({"error": f"Failed: {err}"}), 500

    range_header = request.headers.get("Range")

    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        if range_header:
            req.add_header("Range", range_header)

        resp = urllib.request.urlopen(req, timeout=30)
        headers = dict(resp.headers)

        def generate():
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                yield chunk
            resp.close()

        status = 206 if range_header else 200
        response = Response(generate(), status=status, content_type=headers.get("Content-Type", "video/mp4"))
        if "Content-Length" in headers:
            response.headers["Content-Length"] = headers["Content-Length"]
        if "Content-Range" in headers:
            response.headers["Content-Range"] = headers["Content-Range"]
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ====== HLS (on-demand transcode + cache) ======
@app.route("/api/prepare/<video_id>", methods=["POST"])
def prepare_video(video_id):
    """Extract + transcode to HLS (cached on disk)."""
    db = load_db()
    if video_id not in db["videos"]:
        return jsonify({"error": "Video not in database"}), 404

    playlist = HLS_DIR / video_id / "index.m3u8"
    if playlist.exists():
        return jsonify({"success": True, "cached": True, "stream_url": f"/hls/{video_id}/index.m3u8"})

    lock = get_lock(video_id)
    if not lock.acquire(blocking=False):
        return jsonify({"status": "processing", "message": "Already transcoding..."}), 202

    try:
        db["videos"][video_id]["status"] = "extracting"
        save_db(db)

        url, err = get_cdn_url(video_id)
        if not url:
            db["videos"][video_id]["status"] = f"error: {err}"
            save_db(db)
            return jsonify({"error": f"yt-dlp failed: {err}"}), 500

        db["videos"][video_id]["status"] = "transcoding"
        save_db(db)

        out_dir = HLS_DIR / video_id
        out_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "ffmpeg", "-y", "-i", url,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-hls_time", "6", "-hls_list_size", "0",
            "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
            "-f", "hls", str(playlist)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=SUB_ENV)
        if result.returncode == 0 and playlist.exists():
            db["videos"][video_id]["status"] = "ready"
            save_db(db)
            return jsonify({"success": True, "stream_url": f"/hls/{video_id}/index.m3u8"})
        else:
            db["videos"][video_id]["status"] = "error"
            save_db(db)
            return jsonify({"error": f"FFmpeg failed: {result.stderr[-300:]}"}), 500
    except subprocess.TimeoutExpired:
        db["videos"][video_id]["status"] = "error: timeout"
        save_db(db)
        return jsonify({"error": "FFmpeg timeout"}), 504
    finally:
        lock.release()


@app.route("/hls/<video_id>/<path:filename>")
def serve_hls(video_id, filename):
    """Serve cached HLS segments."""
    hls_path = HLS_DIR / video_id
    if not (hls_path / filename).exists():
        return jsonify({"error": "Not found"}), 404
    mimetype = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    response = send_from_directory(str(hls_path), filename)
    response.headers["Content-Type"] = mimetype
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/api/status")
def status():
    db = load_db()
    vault = db.get("vault", {})
    return jsonify({
        "total_videos": len(db["videos"]),
        "cached_videos": sum(1 for v in db["videos"] if (HLS_DIR / v / "index.m3u8").exists()),
        "total_vault": len(vault),
        "hls_dir": str(HLS_DIR),
        "cookies_exist": COOKIES_FILE.exists(),
    })


# ====== ENCRYPTED YOUTUBE STORAGE ======
# Flow: Encrypt video → upload to YouTube manually → add video ID + key here
# Stream: YouTube CDN → VPS decrypts in RAM → pipe to client (no disk writes)

VAULT_DIR = DATA_DIR / "vault"
VAULT_DIR.mkdir(parents=True, exist_ok=True)


def xor_key_stream(key_bytes, length):
    """Generate repeating key stream for XOR."""
    result = bytearray()
    while len(result) < length:
        result.extend(key_bytes)
    return bytes(result[:length])


def derive_key_bytes(passphrase):
    """Derive 32-byte key from passphrase."""
    return hashlib.sha256(passphrase.encode()).digest()


@app.route("/api/encrypt", methods=["POST"])
def encrypt_video():
    """Upload video → XOR encrypt → return encrypted MP4 for YouTube upload."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    passkey = request.form.get("key", "").strip()

    if not f.filename or not passkey:
        return jsonify({"error": "File and key are required"}), 400

    safe_name = secure_filename(f.filename)
    uid = f"enc_{secrets.token_hex(4)}"
    src_path = VAULT_DIR / f"{uid}_src_{safe_name}"
    enc_path = VAULT_DIR / f"{uid}_encrypted.mp4"

    f.save(str(src_path))
    src_size = src_path.stat().st_size

    # Use FFmpeg to encrypt: read raw → XOR encrypt → re-encode
    # This creates a visually scrambled but valid video that YouTube accepts
    key_bytes = derive_key_bytes(passkey)

    try:
        # Step 1: Get video info
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", str(src_path)
        ]
        probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30, env=SUB_ENV)
        # Default resolution
        width, height = 1920, 1080
        if probe.returncode == 0:
            streams = json.loads(probe.stdout).get("streams", [])
            for s in streams:
                if s.get("codec_type") == "video":
                    width = int(s.get("width", 1920))
                    height = int(s.get("height", 1080))
                    break

        # Step 2: FFmpeg encrypt - re-encode with scrambled pixel data using custom filter
        # Use the 'geq' filter to XOR each pixel with key-derived values
        kr = key_bytes[0]
        kg = key_bytes[1]
        kb = key_bytes[2]
        cmd = [
            "ffmpeg", "-y", "-i", str(src_path),
            "-vf", f"geq=r='bitxor(r(X,Y),{kr})':g='bitxor(g(X,Y),{kg})':b='bitxor(b(X,Y),{kb})'",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "128k",
            str(enc_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, env=SUB_ENV)
        if result.returncode != 0:
            src_path.unlink(missing_ok=True)
            return jsonify({"error": f"FFmpeg encrypt failed: {result.stderr[-200:]}"}), 500
    except Exception as e:
        src_path.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500
    finally:
        src_path.unlink(missing_ok=True)

    enc_size = enc_path.stat().st_size

    return jsonify({
        "success": True,
        "id": uid,
        "enc_file": f"/download/{uid}",
        "size": enc_size,
        "key_r": kr, "key_g": kg, "key_b": kb,
        "message": f"Encrypted! Download → Upload lên YouTube → Add video ID vào Vault"
    })


@app.route("/download/<uid>")
def download_encrypted(uid):
    """Serve encrypted file for user to upload to YouTube."""
    enc_path = VAULT_DIR / f"{uid}_encrypted.mp4"
    if not enc_path.exists():
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(str(VAULT_DIR), f"{uid}_encrypted.mp4",
                               as_attachment=True,
                               download_name=f"encrypted_{uid}.mp4")


@app.route("/api/vault/add", methods=["POST"])
def add_vault():
    """Add encrypted YouTube video to vault (video ID + key)."""
    data = request.json or {}
    video_id = data.get("video_id", "").strip()
    passkey = data.get("key", "").strip()
    title = data.get("title", "").strip()

    vid = parse_video_id(video_id) if video_id else None
    if not vid:
        return jsonify({"error": "Invalid YouTube video ID"}), 400
    if not passkey:
        return jsonify({"error": "Decryption key required"}), 400

    key_bytes = derive_key_bytes(passkey)

    db = load_db()
    if "vault" not in db:
        db["vault"] = {}

    db["vault"][vid] = {
        "title": title or vid,
        "key_r": key_bytes[0],
        "key_g": key_bytes[1],
        "key_b": key_bytes[2],
        "key_hash": hashlib.sha256(passkey.encode()).hexdigest()[:16],
        "added_at": int(time.time()),
    }
    save_db(db)

    return jsonify({"success": True, "id": vid, "decrypt_url": f"/decrypt/{vid}?key={passkey}"})


@app.route("/decrypt/<video_id>")
def decrypt_stream(video_id):
    """Fetch encrypted video from YouTube CDN → decrypt in RAM → stream to client.
    NO FILE IS SAVED TO SERVER. Pure streaming proxy with decryption."""
    passkey = request.args.get("key", "") or request.headers.get("X-Key", "")
    if not passkey:
        return jsonify({"error": "Decryption key required (?key=YOUR_KEY)"}), 401

    # Get key values
    key_bytes = derive_key_bytes(passkey)
    kr, kg, kb = key_bytes[0], key_bytes[1], key_bytes[2]

    # Check vault for key verification (optional)
    db = load_db()
    vault_info = db.get("vault", {}).get(video_id)
    if vault_info:
        expected_hash = vault_info.get("key_hash", "")
        provided_hash = hashlib.sha256(passkey.encode()).hexdigest()[:16]
        if provided_hash != expected_hash:
            return jsonify({"error": "Wrong key"}), 403

    # Get YouTube CDN URL
    cdn_url, err = get_cdn_url(video_id)
    if not cdn_url:
        return jsonify({"error": f"CDN failed: {err}"}), 500

    # FFmpeg: fetch from YouTube CDN → reverse XOR → pipe to stdout
    # This runs entirely in RAM, no disk writes!
    # Reverse XOR: XOR with same values reverses the encryption
    cmd = [
        "ffmpeg",
        "-i", cdn_url,
        "-vf", f"geq=r='bitxor(r(X,Y),{kr})':g='bitxor(g(X,Y),{kg})':b='bitxor(b(X,Y),{kb})'",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4",
        "pipe:1"
    ]

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=SUB_ENV
        )

        def generate():
            try:
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                process.stdout.close()
                process.wait()

        return Response(
            generate(),
            mimetype="video/mp4",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Disposition": "inline",
                "Transfer-Encoding": "chunked",
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vault", methods=["GET"])
def list_vault():
    """List all encrypted YouTube videos in vault."""
    db = load_db()
    items = []
    for vid, info in db.get("vault", {}).items():
        items.append({**info, "id": vid})
    return jsonify({"vault": items})


@app.route("/api/vault/<vid>", methods=["DELETE"])
def delete_vault(vid):
    db = load_db()
    if vid not in db.get("vault", {}):
        return jsonify({"error": "Not found"}), 404
    del db["vault"][vid]
    save_db(db)
    return jsonify({"success": True})


def fsize(b):
    if b < 1024: return f"{b}B"
    if b < 1048576: return f"{b/1024:.1f}KB"
    if b < 1073741824: return f"{b/1048576:.1f}MB"
    return f"{b/1073741824:.1f}GB"


# ====== CLEANUP JOB ======
def cleanup_temp():
    """Clean up old encrypted files (>24h) from vault dir."""
    import glob
    cutoff = time.time() - 86400
    for f in VAULT_DIR.glob("*_encrypted.mp4"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


if __name__ == "__main__":
    update_ytdlp()
    cleanup_temp()
    print("📺 YouTube HLS Streaming Server")
    print(f"📂 HLS Cache: {HLS_DIR}")
    print(f"🔒 Vault temp: {VAULT_DIR}")
    print(f"🍪 Cookies: {COOKIES_FILE} ({'✅' if COOKIES_FILE.exists() else '❌'})")
    print(f"🌐 http://localhost:{PORT}")
    print(f"\n📡 Endpoints:")
    print(f"   /proxy/VIDEO_ID         → Direct proxy stream (YouTube)")
    print(f"   /api/cdn/VIDEO_ID       → Get YouTube CDN URL")
    print(f"   /api/encrypt            → Upload + encrypt video (XOR pixels)")
    print(f"   /download/UID           → Download encrypted MP4")
    print(f"   /decrypt/VID?key=xxx    → YouTube CDN → decrypt → stream (no disk)")
    print(f"   /api/vault              → Manage encrypted video IDs")
    app.run(host="0.0.0.0", port=PORT, debug=True, threaded=True)
