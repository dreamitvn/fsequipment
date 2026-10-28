# FS Equipment Video Automation

Hệ thống tạo video marketing dọc cho FS Equipment từ URL bài viết/sản phẩm, văn bản hoặc tài liệu Word. Pipeline dùng Gemini tạo kịch bản, Vbee hoặc Edge-TTS tạo giọng đọc, Faster-Whisper tạo phụ đề và FFmpeg dựng video. Telegram bot hỗ trợ nhập nội dung, quản lý tư liệu và tùy chọn đăng lên YouTube/Facebook; API và dashboard phục vụ quản lý video.

## Triển khai Ubuntu

Hướng dẫn cài Ubuntu 22.04/24.04, cài thư viện hệ thống/Python, cấu hình `.env`, chạy bot/API và thiết lập systemd nằm trong [UBUNTU.md](./UBUNTU.md).

Các file chính:

| File | Chức năng |
|---|---|
| `pipeline.py` | Trích xuất nội dung, tạo script/audio/phụ đề, phân bổ tư liệu và render video |
| `telegram_bot.py` | Bot Telegram, quét sản phẩm/tin âm thanh, nhận media, render và đăng |
| `server.py` | API FastAPI cho dashboard |
| `AI-dashboard.html` | Giao diện quản lý và thống kê |
| `sync_history.py` | Đồng bộ lịch sử và lượt xem mạng xã hội |
| `youtube_uploader.py` | Xác thực OAuth và upload YouTube |
| `facebook_uploader.py` | Upload Reels qua Meta Graph API |
| `zalo_uploader.py` | Tích hợp upload video Zalo OA, chưa nối vào luồng chính |

## Bảo mật và vận hành

- Không commit `.env`, `assets/client_secrets.json` hoặc `assets/youtube_token.json`; các mục này đã được thêm vào `.gitignore`.
- Các token từng có trong mã/log cần được thu hồi và cấp lại. `.env.example` chỉ là mẫu rỗng, không dùng token của máy cũ.
- Chỉ chạy một tiến trình polling cho mỗi Telegram bot token; chạy trùng sẽ gây `409 Conflict`.
- Bot và API cùng dùng thư mục `temp`, tệp video đầu ra và `videos_data.json`. Tránh render đồng thời từ nhiều luồng hoặc máy dùng chung thư mục.
- API chưa có authentication. Mặc định giữ `API_HOST=127.0.0.1`; không công khai cổng API trên Internet.
- `assets/banner_frame.png` cần có trên máy chạy pipeline. Các token/OAuth phải được cấu hình riêng trên Ubuntu.

## Dependencies

Các thư viện Python được khai báo trong [requirements.txt](./requirements.txt). FFmpeg và font là dependencies hệ thống, được cài theo hướng dẫn tại [UBUNTU.md](./UBUNTU.md).
