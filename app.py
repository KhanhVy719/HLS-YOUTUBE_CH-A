#!/usr/bin/env python3
"""YouTube HLS Streaming Server — Proxy YouTube CDN as HLS streams."""

import os
import json
import re
import subprocess
import time
import threading
import urllib.request
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response, redirect

app = Flask(__name__, static_folder="static")
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

# ============ CONFIG ============
DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/yt-hls"))
HLS_DIR = DATA_DIR / "hls"
DB_FILE = DATA_DIR / "videos.json"
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/root/cookies.txt"))
PORT = int(os.environ.get("PORT", "5555"))
DENO_DIR = os.path.expanduser("~/.deno/bin")

# Subprocess env with deno in PATH
SUB_ENV = os.environ.copy()
SUB_ENV["PATH"] = DENO_DIR + ":" + SUB_ENV.get("PATH", "")

HLS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

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
def get_cdn_url(video_id, force=False):
    """Get YouTube CDN direct URL via yt-dlp, with caching."""
    # Check cache
    if not force and video_id in _url_cache:
        entry = _url_cache[video_id]
        if time.time() < entry["expires"]:
            return entry["url"], None

    cmd = [
        "yt-dlp", "-g", "-f", "best[ext=mp4]/best",
        "--remote-components", "ejs:github",
    ]
    if COOKIES_FILE.exists():
        cmd.extend(["--cookies", str(COOKIES_FILE)])
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=SUB_ENV)
        if result.returncode == 0:
            url = result.stdout.strip().split('\n')[0]
            _url_cache[video_id] = {"url": url, "expires": time.time() + URL_TTL}
            return url, None
        return None, result.stderr.strip()[-300:]
    except subprocess.TimeoutExpired:
        return None, "yt-dlp timeout"


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
    return jsonify({
        "total_videos": len(db["videos"]),
        "cached_videos": sum(1 for v in db["videos"] if (HLS_DIR / v / "index.m3u8").exists()),
        "hls_dir": str(HLS_DIR),
        "cookies_exist": COOKIES_FILE.exists(),
    })


if __name__ == "__main__":
    print("📺 YouTube HLS Streaming Server")
    print(f"📂 HLS Cache: {HLS_DIR}")
    print(f"🍪 Cookies: {COOKIES_FILE} ({'✅' if COOKIES_FILE.exists() else '❌'})")
    print(f"🌐 http://localhost:{PORT}")
    print(f"\n📡 Endpoints:")
    print(f"   /proxy/VIDEO_ID     → Direct proxy stream (no download)")
    print(f"   /api/cdn/VIDEO_ID   → Get YouTube CDN URL")
    print(f"   /hls/VIDEO_ID/      → HLS m3u8 (after prepare)")
    app.run(host="0.0.0.0", port=PORT, debug=True, threaded=True)
