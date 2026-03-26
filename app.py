#!/usr/bin/env python3
"""YouTube HLS Streaming Server — Use YouTube as free video CDN."""

import os
import json
import subprocess
import hashlib
import time
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response

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

# Transcoding lock per video
_locks = {}
_lock_master = threading.Lock()


# ============ DATABASE (JSON file) ============
def load_db():
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {"videos": {}}


def save_db(db):
    DB_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False))


# ============ HELPERS ============
def get_lock(video_id):
    with _lock_master:
        if video_id not in _locks:
            _locks[video_id] = threading.Lock()
        return _locks[video_id]


def extract_youtube_url(video_id):
    """Use yt-dlp to get direct video URL from YouTube."""
    cmd = ["yt-dlp", "-g", "-f", "best[ext=mp4]/best", "--remote-components", "ejs:github"]
    if COOKIES_FILE.exists():
        cmd.extend(["--cookies", str(COOKIES_FILE)])
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=SUB_ENV)
        if result.returncode == 0:
            url = result.stdout.strip().split('\n')[0]
            return url, None
        return None, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return None, "yt-dlp timeout"


def transcode_to_hls(video_id, source_url):
    """Transcode YouTube video to HLS segments using FFmpeg."""
    out_dir = HLS_DIR / video_id
    playlist = out_dir / "index.m3u8"

    # Already cached?
    if playlist.exists():
        return True, None

    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", source_url,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-hls_time", "6",
        "-hls_list_size", "0",
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
        "-f", "hls",
        str(playlist)
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0 and playlist.exists():
            return True, None
        return False, result.stderr[-500:] if result.stderr else "Unknown error"
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timeout (>10 min)"


def get_video_info(video_id):
    """Get video title and metadata from yt-dlp."""
    cmd = ["yt-dlp", "--print", "title", "--print", "duration", "--print", "thumbnail", "--no-download", "--remote-components", "ejs:github"]
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
        videos.append({**info, "id": vid, "cached": cached})
    return jsonify({"videos": videos})


@app.route("/api/videos", methods=["POST"])
def add_video():
    data = request.json or {}
    video_id = data.get("video_id", "").strip()

    # Extract video ID from various URL formats
    if "youtube.com" in video_id or "youtu.be" in video_id:
        import re
        match = re.search(r'(?:v=|\/live\/|youtu\.be\/)([a-zA-Z0-9_-]{11})', video_id)
        if match:
            video_id = match.group(1)

    if not video_id or len(video_id) != 11:
        return jsonify({"error": "Invalid YouTube video ID"}), 400

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

    return jsonify({"success": True})


@app.route("/api/prepare/<video_id>", methods=["POST"])
def prepare_video(video_id):
    """Extract YouTube URL and transcode to HLS (can take a while)."""
    db = load_db()
    if video_id not in db["videos"]:
        return jsonify({"error": "Video not in database"}), 404

    # Check cache
    playlist = HLS_DIR / video_id / "index.m3u8"
    if playlist.exists():
        return jsonify({"success": True, "cached": True, "stream_url": f"/hls/{video_id}/index.m3u8"})

    lock = get_lock(video_id)
    if not lock.acquire(blocking=False):
        return jsonify({"status": "processing", "message": "Already transcoding..."}), 202

    try:
        # Update status
        db["videos"][video_id]["status"] = "extracting"
        save_db(db)

        # Extract URL
        url, err = extract_youtube_url(video_id)
        if not url:
            db["videos"][video_id]["status"] = f"error: {err}"
            save_db(db)
            return jsonify({"error": f"yt-dlp failed: {err}"}), 500

        # Transcode
        db["videos"][video_id]["status"] = "transcoding"
        save_db(db)

        ok, err = transcode_to_hls(video_id, url)
        if not ok:
            db["videos"][video_id]["status"] = f"error: {err}"
            save_db(db)
            return jsonify({"error": f"FFmpeg failed: {err}"}), 500

        db["videos"][video_id]["status"] = "ready"
        save_db(db)

        return jsonify({
            "success": True,
            "stream_url": f"/hls/{video_id}/index.m3u8"
        })
    finally:
        lock.release()


@app.route("/hls/<video_id>/<path:filename>")
def serve_hls(video_id, filename):
    """Serve HLS playlist and segments."""
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
    app.run(host="0.0.0.0", port=PORT, debug=True, threaded=True)
