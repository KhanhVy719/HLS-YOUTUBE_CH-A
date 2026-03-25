# HLS-YOUTUBE — YT Media Storage Web GUI

Web GUI cho [PulseBeat02/yt-media-storage](https://github.com/PulseBeat02/yt-media-storage) — Tool biến YouTube thành Cloud Storage.

## 🚀 Cách hoạt động

```
File gốc → Encode (Fountain Codes + FFV1) → Video MKV → Upload YouTube → Tải về → Decode → File gốc
```

## 📦 Cài đặt

### 1. Build yt-media-storage (Ubuntu)

```bash
sudo apt update && sudo apt install -y cmake build-essential qt6-base-dev \
  libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libswresample-dev \
  libsodium-dev libomp-dev ffmpeg

git clone https://github.com/PulseBeat02/yt-media-storage.git ~/yt-media-storage
cd ~/yt-media-storage
cmake -B build
cmake --build build -j$(nproc)
```

### 2. Chạy Web GUI

```bash
git clone https://github.com/KhanhVy719/HLS-YOUTUBE_CH-A.git ~/yt-web
cd ~/yt-web
pip install flask
python3 app.py
```

Mở browser: `http://YOUR_IP:5555`

## 🎯 Tính năng

- 📦 **Encode** — Kéo thả file → biến thành video MKV (lossless)
- 🔓 **Decode** — Kéo thả MKV → khôi phục file gốc
- 🔐 **Encryption** — Mã hóa XChaCha20-Poly1305
- ⬇️ **Download** — Tải kết quả trực tiếp

## ⚙️ Cấu hình

| Biến môi trường | Mặc định | Mô tả |
|-----------------|----------|-------|
| `MEDIA_STORAGE_BIN` | `~/yt-media-storage/build/media_storage` | Đường dẫn binary |

```bash
MEDIA_STORAGE_BIN=/custom/path/media_storage python3 app.py
```

## 📁 Cấu trúc

```
├── app.py              # Flask backend
├── requirements.txt    # Dependencies
├── static/
│   └── index.html      # Web UI
└── README.md
```
