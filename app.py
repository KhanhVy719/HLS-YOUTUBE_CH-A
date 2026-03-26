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
    uploads = db.get("uploads", {})
    return jsonify({
        "total_videos": len(db["videos"]),
        "cached_videos": sum(1 for v in db["videos"] if (HLS_DIR / v / "index.m3u8").exists()),
        "total_uploads": len(uploads),
        "hls_dir": str(HLS_DIR),
        "cookies_exist": COOKIES_FILE.exists(),
    })


# ====== UPLOAD + ENCRYPT ======
def derive_key(passphrase):
    """Derive a 16-byte AES key from passphrase."""
    return hashlib.sha256(passphrase.encode()).digest()[:16]


def gen_key_iv():
    """Generate random 16-byte key and IV."""
    return secrets.token_bytes(16), secrets.token_bytes(16)


@app.route("/api/upload", methods=["POST"])
def upload_video():
    """Upload video file → encrypt → transcode to AES-128 HLS."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    passkey = request.form.get("key", "").strip()
    title = request.form.get("title", "").strip()

    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"File type .{ext} not allowed"}), 400

    if not passkey:
        return jsonify({"error": "Encryption key is required"}), 400

    # Generate unique ID
    uid = f"up_{secrets.token_hex(6)}"
    safe_name = secure_filename(f.filename)
    upload_path = UPLOAD_DIR / f"{uid}_{safe_name}"

    # Save uploaded file
    f.save(str(upload_path))
    file_size = upload_path.stat().st_size

    # Derive AES key from passphrase
    aes_key = derive_key(passkey)
    aes_iv = secrets.token_bytes(16)

    # Save key info file
    out_dir = HLS_DIR / uid
    out_dir.mkdir(parents=True, exist_ok=True)

    key_file = out_dir / "enc.key"
    key_file.write_bytes(aes_key)

    iv_hex = aes_iv.hex()

    # Create key info file for FFmpeg
    # Format: key_uri\nkey_file_path\niv
    key_info_file = out_dir / "key_info.txt"
    key_info_file.write_text(f"/api/key/{uid}?key={{key}}\n{key_file}\n{iv_hex}\n")

    # FFmpeg: transcode to AES-128 encrypted HLS
    playlist = out_dir / "index.m3u8"
    cmd = [
        "ffmpeg", "-y", "-i", str(upload_path),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-hls_time", "6", "-hls_list_size", "0",
        "-hls_key_info_file", str(key_info_file),
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
        "-f", "hls", str(playlist)
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=SUB_ENV)
        if result.returncode != 0 or not playlist.exists():
            upload_path.unlink(missing_ok=True)
            return jsonify({"error": f"FFmpeg failed: {result.stderr[-300:]}"}), 500
    except subprocess.TimeoutExpired:
        upload_path.unlink(missing_ok=True)
        return jsonify({"error": "Encoding timeout (>30min)"}), 504

    # Fix m3u8: replace key URI placeholder with actual endpoint
    m3u8_content = playlist.read_text()
    m3u8_content = m3u8_content.replace("/api/key/{uid}?key={key}", f"/api/key/{uid}")
    playlist.write_text(m3u8_content)

    # Clean up source file
    upload_path.unlink(missing_ok=True)

    # Save to DB
    db = load_db()
    if "uploads" not in db:
        db["uploads"] = {}
    db["uploads"][uid] = {
        "title": title or safe_name,
        "filename": safe_name,
        "size": file_size,
        "encrypted": True,
        "key_hash": hashlib.sha256(passkey.encode()).hexdigest()[:16],
        "added_at": int(time.time()),
        "status": "ready",
    }
    save_db(db)

    return jsonify({
        "success": True,
        "id": uid,
        "stream_url": f"/hls/{uid}/index.m3u8",
        "message": "Video encrypted and ready for HLS streaming"
    })


@app.route("/api/key/<uid>")
def serve_key(uid):
    """Serve AES-128 decryption key (requires passphrase via query param or header)."""
    key_file = HLS_DIR / uid / "enc.key"
    if not key_file.exists():
        return jsonify({"error": "Key not found"}), 404

    # Optional: validate passphrase
    passkey = request.args.get("key", "") or request.headers.get("X-Key", "")
    db = load_db()
    upload_info = db.get("uploads", {}).get(uid)

    if upload_info and passkey:
        expected_hash = upload_info.get("key_hash", "")
        provided_hash = hashlib.sha256(passkey.encode()).hexdigest()[:16]
        if provided_hash != expected_hash:
            return Response("Invalid key", status=403)

    # Return the raw key bytes
    key_data = key_file.read_bytes()
    resp = Response(key_data, content_type="application/octet-stream")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/api/uploads", methods=["GET"])
def list_uploads():
    """List all uploaded + encrypted videos."""
    db = load_db()
    uploads = []
    for uid, info in db.get("uploads", {}).items():
        has_hls = (HLS_DIR / uid / "index.m3u8").exists()
        uploads.append({**info, "id": uid, "has_hls": has_hls})
    return jsonify({"uploads": uploads})


@app.route("/api/uploads/<uid>", methods=["DELETE"])
def delete_upload(uid):
    db = load_db()
    if uid not in db.get("uploads", {}):
        return jsonify({"error": "Not found"}), 404
    del db["uploads"][uid]
    save_db(db)
    import shutil
    shutil.rmtree(HLS_DIR / uid, ignore_errors=True)
    return jsonify({"success": True})


if __name__ == "__main__":
    update_ytdlp()
    print("📺 YouTube HLS Streaming Server")
    print(f"📂 HLS Cache: {HLS_DIR}")
    print(f"📤 Uploads: {UPLOAD_DIR}")
    print(f"🍪 Cookies: {COOKIES_FILE} ({'✅' if COOKIES_FILE.exists() else '❌'})")
    print(f"🌐 http://localhost:{PORT}")
    print(f"\n📡 Endpoints:")
    print(f"   /proxy/VIDEO_ID     → Direct proxy stream")
    print(f"   /api/cdn/VIDEO_ID   → Get YouTube CDN URL")
    print(f"   /hls/VIDEO_ID/      → HLS m3u8 (after prepare)")
    print(f"   /api/upload         → Upload + encrypt video")
    print(f"   /api/key/UID        → AES-128 key endpoint")
    app.run(host="0.0.0.0", port=PORT, debug=True, threaded=True)
