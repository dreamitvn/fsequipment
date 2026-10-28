import os
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"

CLIENT_SECRETS_FILE = str(ASSETS_DIR / "client_secrets.json")
TOKEN_FILE = str(ASSETS_DIR / "youtube_token.json")

# Scope yêu cầu quyền upload video
SCOPES = [
          "https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"
]

def get_authenticated_service():
    """
    Xác thực OAuth 2.0.
    Lần đầu tiên chạy sẽ mở trình duyệt để bạn đăng nhập và cấp quyền.
    Các lần sau token sẽ tự động được lưu vào youtube_token.json.
    """
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                raise FileNotFoundError(f"Không tìm thấy file: {CLIENT_SECRETS_FILE}")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)

def upload_youtube_shorts(video_path: str, title: str, description: str = "", tags: list = None, privacy_status: str = "public"):
    """
    Đăng video lên YouTube Shorts.
    - Để thuật toán nhận diện Shorts ngay lập tức: thêm #Shorts vào cuối title và description.
    - privacy_status: 'public', 'private', hoặc 'unlisted'
    """
    if not os.path.exists(video_path):
        print(f"❌ File video không tồn tại: {video_path}")
        return None

    # Tối ưu hóa tiêu đề và mô tả cho Shorts (giới hạn tiêu đề YouTube là 100 ký tự)
    clean_title = title[:80].strip()
    if "#Shorts" not in clean_title and "#shorts" not in clean_title:
        clean_title = f"{clean_title}"

    clean_description = f"{description}\n\n#Shorts #Tintuc #Hoanhap"

    default_tags = ["Shorts", "Tin tức", "Thời sự", "Hòa Nhập"]
    if tags:
        default_tags.extend(tags)

    print("🔑 Đang khởi tạo kết nối YouTube API...")
    youtube = get_authenticated_service()

    body = {
        "snippet": {
            "title": clean_title,
            "description": clean_description,
            "tags": default_tags,
            "categoryId": "25"  # Category 25: News & Politics (Tin tức & Chính trị)
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False
        }
    }

    # Upload dạng chunked để đường truyền ổn định
    media = MediaFileUpload(
        video_path,
        chunksize=1024 * 1024 * 2,  # 2MB mỗi chunk
        resumable=True,
        mimetype="video/mp4"
    )

    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media
    )

    print(f"🚀 Đang tải video lên YouTube Shorts: '{clean_title}'...")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  -> Tiến trình upload: {int(status.progress() * 100)}%")

    video_id = response.get("id")
    video_url = f"https://youtube.com/shorts/{video_id}"
    print(f"✅ Đăng tải thành công! Xem Shorts tại: {video_url}")
    return video_url