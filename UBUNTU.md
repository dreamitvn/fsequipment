# Chạy hệ thống trên Ubuntu

Hướng dẫn này dành cho Ubuntu 22.04/24.04 và mã nguồn hiện tại. Chạy bot bằng `telegram_bot.py`; không chạy nhiều tiến trình với cùng một token Telegram.

## 1. Cài chương trình hệ thống

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip ffmpeg fonts-dejavu-core fonts-liberation
```

Kiểm tra:

```bash
python3 --version
ffmpeg -version
ffprobe -version
```

FFmpeg cần encoder H.264 (`libx264`) và hỗ trợ filter subtitles/libass. Gói FFmpeg của Ubuntu thường có sẵn; nếu bản cài thiếu filter/encoder tương ứng, cần cài bản FFmpeg đầy đủ trước khi render. Font DejaVu Sans được dùng để vẽ tiêu đề.

## 2. Chuyển mã nguồn và tài nguyên

Chỉ clone repository sau khi đã loại credential khỏi mã nguồn và lịch sử Git:

```bash
git clone <URL_REPOSITORY_RIENG_TU>
cd <TEN_THU_MUC_DU_AN>
```

Hoặc chuyển bản mã đã làm sạch thủ công. Cần có `assets/banner_frame.png`; không chuyển `.env`, OAuth client/token, video, database hoặc dữ liệu cá nhân từ máy cũ. Tạo thư mục và dữ liệu dashboard nếu chưa có:

```bash
mkdir -p assets temp
test -f assets/banner_frame.png || echo "Thiếu assets/banner_frame.png"
test -f videos_data.json || printf '[]\n' > videos_data.json
```

## 3. Tạo môi trường Python và cài thư viện

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`requirements.txt` cài các gói Python cần cho pipeline, bot, API và đồng bộ:

- Nội dung/ảnh/Word: `requests`, `beautifulsoup4`, `Pillow`, `python-docx`
- Gemini, giọng đọc và phụ đề: `google-genai`, `edge-tts`, `faster-whisper`
- Telegram/RSS: `python-telegram-bot`, `feedparser`
- API: `fastapi`, `uvicorn`
- YouTube/đồng bộ: `google-auth-oauthlib`, `google-api-python-client`, `yt-dlp`
- Cấu hình và tiện ích: `python-dotenv`, `schedule`

`faster-whisper` tải model `base` khi tạo phụ đề lần đầu; cần mạng và dung lượng đĩa. `yt-dlp` được cài vào virtualenv, nên lệnh `yt-dlp` sẽ có trong `PATH` khi virtualenv được kích hoạt.

## 4. Cấu hình dịch vụ

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Điền credential mới do bạn quản lý, không tái sử dụng token đã từng xuất hiện trong mã hoặc log:

- `GEMINI_API_KEY`: cần để tạo kịch bản.
- `TELEGRAM_BOT_TOKEN`: cần để chạy bot.
- `ALLOWED_CHAT_ID`: danh sách chat ID được phép, phân cách bằng dấu phẩy.
- `VBEE_APP_ID` và `VBEE_API_TOKEN`: tùy chọn; để trống thì dùng Edge-TTS.
- `FB_PAGE_ID` và `FB_ACCESS_TOKEN`: cần nếu đăng hoặc đồng bộ Facebook.
- `PIXABAY_API_KEY`, `ZALO_OA_ACCESS_TOKEN`: tùy chọn theo tính năng.
- YouTube cần OAuth client riêng tại `assets/client_secrets.json`; lần xác thực tạo `assets/youtube_token.json`.

Không commit `.env` hoặc OAuth. API dashboard chưa có xác thực; giữ `API_HOST=127.0.0.1` và không mở cổng 8000 trực tiếp ra Internet.

## 5. Kiểm tra và chạy

```bash
source .venv/bin/activate
python -m compileall -q .
python -c "import pipeline, telegram_bot, server; print('Import OK')"
```

Chạy bot:

```bash
source .venv/bin/activate
python telegram_bot.py
```

Chỉ chạy một bot polling cho mỗi token. Nếu cùng token đang chạy ở máy/tiến trình khác, Telegram trả `409 Conflict`.

Chạy API cục bộ trong terminal riêng:

```bash
source .venv/bin/activate
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Để chạy dashboard trên cùng máy, mở terminal khác:

```bash
python3 -m http.server 5500 --bind 127.0.0.1
```

Mở `http://127.0.0.1:5500/AI-dashboard.html`. Không đưa API lên Internet nếu chưa thêm authentication, CORS allowlist và reverse proxy được bảo vệ.

Pipeline dòng lệnh:

```bash
source .venv/bin/activate
python pipeline.py "Nội dung cần làm video"
python pipeline.py "https://example.com/bai-viet"
python pipeline.py "/duong-dan/toi/bai-viet.docx"
```

## 6. Chạy Telegram bot bằng systemd (tùy chọn)

Thay `/opt/fsequipment` và `fsequipment` bằng đường dẫn và tài khoản Linux của bạn. Tạo `/etc/systemd/system/fsequipment-bot.service`:

```ini
[Unit]
Description=FS Equipment Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=fsequipment
WorkingDirectory=/opt/fsequipment
ExecStart=/opt/fsequipment/.venv/bin/python /opt/fsequipment/telegram_bot.py
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
```

Kích hoạt và xem log:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fsequipment-bot
sudo systemctl status fsequipment-bot
sudo journalctl -u fsequipment-bot -f
```

Không chạy bot thủ công song song với systemd. Bot và API cùng dùng thư mục `temp`, video đầu ra và `videos_data.json`; không nên render đồng thời từ hai luồng.

## 7. Khắc phục lỗi

- `ffmpeg: command not found`: cài `ffmpeg` bằng apt và kiểm tra `PATH`.
- Không tìm thấy font: cài `fonts-dejavu-core` hoặc `fonts-liberation`.
- Thiếu banner: đặt `banner_frame.png` tại `assets/banner_frame.png`.
- Lỗi Whisper lần đầu: kiểm tra kết nối mạng và dung lượng ổ đĩa để tải model.
- `telegram.error.Conflict`: dừng các tiến trình bot khác, giữ một tiến trình polling.
- YouTube cần xác thực: dùng OAuth client của bạn và giữ token được tạo trên server an toàn.
- SSH/VPS: dùng systemd để giữ bot chạy; truy cập dashboard/API qua SSH tunnel hoặc reverse proxy có xác thực.
