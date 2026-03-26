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


# ====== ENCRYPTED VAULT ======
VAULT_DIR = DATA_DIR / "vault"
VAULT_DIR.mkdir(parents=True, exist_ok=True)
CHUNK_SIZE = 64 * 1024  # 64KB chunks


def derive_aes_key(passphrase):
    """Derive 32-byte AES-256 key + 16-byte IV from passphrase."""
    key = hashlib.sha256(passphrase.encode()).digest()  # 32 bytes
    iv = hashlib.md5(passphrase.encode()).digest()       # 16 bytes
    return key, iv


def encrypt_file(src_path, dst_path, passphrase):
    """Encrypt file with AES-256-CTR, chunk by chunk."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key, iv = derive_aes_key(passphrase)
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    encryptor = cipher.encryptor()

    with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
        while True:
            chunk = fin.read(CHUNK_SIZE)
            if not chunk:
                break
            fout.write(encryptor.update(chunk))
        fout.write(encryptor.finalize())


def decrypt_stream(enc_path, passphrase):
    """Generator: decrypt file in chunks for streaming."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key, iv = derive_aes_key(passphrase)
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    decryptor = cipher.decryptor()

    with open(enc_path, "rb") as fin:
        while True:
            chunk = fin.read(CHUNK_SIZE)
            if not chunk:
                break
            yield decryptor.update(chunk)
        final = decryptor.finalize()
        if final:
            yield final


@app.route("/api/upload", methods=["POST"])
def upload_video():
    """Upload video → AES-256 encrypt → store .enc on disk."""
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
    uid = f"v_{secrets.token_hex(6)}"
    safe_name = secure_filename(f.filename)
    tmp_path = UPLOAD_DIR / f"{uid}_{safe_name}"
    enc_path = VAULT_DIR / f"{uid}.enc"

    # Save uploaded file temporarily
    f.save(str(tmp_path))
    file_size = tmp_path.stat().st_size

    # Encrypt and save
    try:
        encrypt_file(tmp_path, enc_path, passkey)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        return jsonify({"error": f"Encryption failed: {str(e)}"}), 500
    finally:
        # Always delete original (unencrypted)
        tmp_path.unlink(missing_ok=True)

    enc_size = enc_path.stat().st_size

    # Save to DB  (key hash for verification, NOT the key itself)
    db = load_db()
    if "vault" not in db:
        db["vault"] = {}
    db["vault"][uid] = {
        "title": title or safe_name,
        "filename": safe_name,
        "ext": ext,
        "original_size": file_size,
        "enc_size": enc_size,
        "key_hash": hashlib.sha256(passkey.encode()).hexdigest()[:16],
        "added_at": int(time.time()),
    }
    save_db(db)

    return jsonify({
        "success": True,
        "id": uid,
        "stream_url": f"/stream/{uid}",
        "message": f"Encrypted & stored ({fsize(file_size)} → {fsize(enc_size)})"
    })


@app.route("/stream/<uid>")
def stream_decrypted(uid):
    """Decrypt .enc file on-the-fly and stream as video."""
    db = load_db()
    info = db.get("vault", {}).get(uid)
    if not info:
        return jsonify({"error": "Not found"}), 404

    enc_path = VAULT_DIR / f"{uid}.enc"
    if not enc_path.exists():
        return jsonify({"error": "Encrypted file missing"}), 404

    # Get key from query param or header
    passkey = request.args.get("key", "") or request.headers.get("X-Key", "")
    if not passkey:
        return jsonify({"error": "Decryption key required. Use ?key=YOUR_KEY"}), 401

    # Verify key
    expected_hash = info.get("key_hash", "")
    provided_hash = hashlib.sha256(passkey.encode()).hexdigest()[:16]
    if provided_hash != expected_hash:
        return jsonify({"error": "Wrong decryption key"}), 403

    # Determine MIME type
    ext = info.get("ext", "mp4")
    mime_map = {"mp4": "video/mp4", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
                "mov": "video/quicktime", "webm": "video/webm", "flv": "video/x-flv",
                "ts": "video/mp2t", "m4v": "video/mp4"}
    mime = mime_map.get(ext, "video/mp4")

    response = Response(
        decrypt_stream(enc_path, passkey),
        mimetype=mime,
        headers={
            "Content-Length": str(info["original_size"]),
            "Accept-Ranges": "none",
            "Access-Control-Allow-Origin": "*",
            "Content-Disposition": "inline",
        }
    )
    return response


@app.route("/api/vault", methods=["GET"])
def list_vault():
    """List all encrypted videos."""
    db = load_db()
    items = []
    for uid, info in db.get("vault", {}).items():
        enc_exists = (VAULT_DIR / f"{uid}.enc").exists()
        items.append({**info, "id": uid, "enc_exists": enc_exists})
    return jsonify({"vault": items})


@app.route("/api/vault/<uid>", methods=["DELETE"])
def delete_vault(uid):
    db = load_db()
    if uid not in db.get("vault", {}):
        return jsonify({"error": "Not found"}), 404
    del db["vault"][uid]
    save_db(db)
    enc_path = VAULT_DIR / f"{uid}.enc"
    enc_path.unlink(missing_ok=True)
    return jsonify({"success": True})


def fsize(b):
    if b < 1024: return f"{b}B"
    if b < 1048576: return f"{b/1024:.1f}KB"
    if b < 1073741824: return f"{b/1048576:.1f}MB"
    return f"{b/1073741824:.1f}GB"


if __name__ == "__main__":
    update_ytdlp()
    print("📺 YouTube HLS Streaming Server")
    print(f"📂 HLS Cache: {HLS_DIR}")
    print(f"🔒 Vault: {VAULT_DIR}")
    print(f"🍪 Cookies: {COOKIES_FILE} ({'✅' if COOKIES_FILE.exists() else '❌'})")
    print(f"🌐 http://localhost:{PORT}")
    print(f"\n📡 Endpoints:")
    print(f"   /proxy/VIDEO_ID       → Direct proxy stream (YouTube)")
    print(f"   /api/cdn/VIDEO_ID     → Get YouTube CDN URL")
    print(f"   /hls/VIDEO_ID/        → HLS m3u8 (after prepare)")
    print(f"   /api/upload           → Upload + encrypt video")
    print(f"   /stream/UID?key=xxx   → Decrypt + stream on-the-fly")
    print(f"   /api/vault            → List encrypted videos")
    app.run(host="0.0.0.0", port=PORT, debug=True, threaded=True)
