#!/usr/bin/env python3
"""Web GUI for yt-media-storage - Encode/Decode files as YouTube videos."""

import os
import subprocess
import uuid
import shutil
import mimetypes
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory, Response

app = Flask(__name__, static_folder="static")
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB upload limit

# Config
UPLOAD_DIR = Path("/tmp/yt-storage-uploads")
OUTPUT_DIR = Path("/tmp/yt-storage-outputs")
MEDIA_STORAGE_BIN = os.environ.get(
    "MEDIA_STORAGE_BIN",
    os.path.expanduser("~/yt-media-storage/build/media_storage")
)

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/encode", methods=["POST"])
def encode_file():
    """Encode a file into a lossless MKV video."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    password = request.form.get("password", "").strip()
    hash_algo = request.form.get("hash", "crc32")

    job_id = str(uuid.uuid4())[:8]
    input_path = UPLOAD_DIR / f"{job_id}_{file.filename}"
    output_path = OUTPUT_DIR / f"{job_id}_encoded.mkv"

    file.save(str(input_path))
    input_size = input_path.stat().st_size

    # Build command
    cmd = [MEDIA_STORAGE_BIN, "encode", "-i", str(input_path), "-o", str(output_path)]
    if hash_algo == "xxhash":
        cmd.extend(["--hash", "xxhash"])
    if password:
        cmd.extend(["--encrypt", "--password", password])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return jsonify({
                "error": "Encode failed",
                "stderr": result.stderr,
                "stdout": result.stdout
            }), 500

        output_size = output_path.stat().st_size

        return jsonify({
            "success": True,
            "job_id": job_id,
            "original_name": file.filename,
            "input_size": input_size,
            "output_size": output_size,
            "download_url": f"/api/download/{job_id}_encoded.mkv",
            "stdout": result.stdout
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Encode timed out (>5 min)"}), 504
    finally:
        input_path.unlink(missing_ok=True)


@app.route("/api/decode", methods=["POST"])
def decode_file():
    """Decode a MKV video back to the original file."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    password = request.form.get("password", "").strip()
    output_name = request.form.get("output_name", "decoded_file").strip()

    job_id = str(uuid.uuid4())[:8]
    input_path = UPLOAD_DIR / f"{job_id}_{file.filename}"
    output_path = OUTPUT_DIR / f"{job_id}_{output_name}"

    file.save(str(input_path))

    cmd = [MEDIA_STORAGE_BIN, "decode", "-i", str(input_path), "-o", str(output_path)]
    if password:
        cmd.extend(["--password", password])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return jsonify({
                "error": "Decode failed",
                "stderr": result.stderr,
                "stdout": result.stdout
            }), 500

        output_size = output_path.stat().st_size

        return jsonify({
            "success": True,
            "job_id": job_id,
            "output_size": output_size,
            "download_url": f"/api/download/{job_id}_{output_name}",
            "stdout": result.stdout
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Decode timed out (>5 min)"}), 504
    finally:
        input_path.unlink(missing_ok=True)


@app.route("/api/download/<path:filename>")
def download_file(filename):
    """Download a processed file."""
    filepath = OUTPUT_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "File not found", "path": str(filepath)}), 404
    return send_from_directory(str(OUTPUT_DIR), filename, as_attachment=True)


@app.route("/api/files")
def list_files():
    """List all files in output directory (debug)."""
    files = []
    for f in OUTPUT_DIR.iterdir():
        files.append({"name": f.name, "size": f.stat().st_size})
    return jsonify({"files": files})


@app.route("/api/status")
def status():
    """Check if media_storage binary exists."""
    binary_exists = os.path.isfile(MEDIA_STORAGE_BIN) and os.access(MEDIA_STORAGE_BIN, os.X_OK)
    return jsonify({
        "binary_path": MEDIA_STORAGE_BIN,
        "binary_exists": binary_exists,
        "upload_dir": str(UPLOAD_DIR),
        "output_dir": str(OUTPUT_DIR),
    })


if __name__ == "__main__":
    print(f"🎬 YT Media Storage Web GUI")
    print(f"📂 Binary: {MEDIA_STORAGE_BIN}")
    print(f"🌐 Open http://localhost:5555")
    app.run(host="0.0.0.0", port=5555, debug=True)
