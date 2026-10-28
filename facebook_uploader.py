import os
import requests
from pathlib import Path

def upload_facebook_reels(video_path: str, caption: str, page_id: str, access_token: str):
    """
    Tự động tải và xuất bản video lên Facebook Reels theo chuẩn Meta Graph API v20.0+.
    """
    if not os.path.exists(video_path):
        print(f"❌ File video không tồn tại: {video_path}")
        return None

    file_size = os.path.getsize(video_path)
    base_url = "https://graph.facebook.com/v20.0"

    print(f"🚀 Bắt đầu quy trình tải Reels ({file_size / (1024*1024):.2f} MB)...")

    # GIAI ĐOẠN 1: Khởi tạo phiên upload
    init_url = f"{base_url}/{page_id}/video_reels"
    init_params = {
        "upload_phase": "start",
        "access_token": access_token
    }

    try:
        r_init = requests.post(init_url, data=init_params, timeout=25)
        init_res = r_init.json()
        if r_init.status_code != 200:
            print(f"❌ Lỗi ở Giai đoạn 1 (Khởi tạo phiên - {r_init.status_code}): {init_res}")
            return None

        video_id = init_res.get("video_id") or init_res.get("id")
        upload_url = init_res.get("upload_url")
        print(f"  -> Khởi tạo thành công: Video ID = {video_id}")
    except Exception as e:
        print(f"❌ Lỗi kết nối Giai đoạn 1: {e}")
        return None

    # GIAI ĐOẠN 2: Truyền dữ liệu file video
    headers = {
        "Authorization": f"OAuth {access_token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream"
    }

    try:
        print("  -> Đang truyền dữ liệu video lên máy chủ Meta...")
        with open(video_path, "rb") as f:
            video_data = f.read()

        r_upload = requests.post(
            upload_url,
            headers=headers,
            data=video_data,
            timeout=180
        )
        if r_upload.status_code not in [200, 201]:
            print(f"❌ Lỗi ở Giai đoạn 2 (Upload dữ liệu - {r_upload.status_code}): {r_upload.text}")
            return None
        print("  -> Tải dữ liệu thành công.")
    except Exception as e:
        print(f"❌ Lỗi kết nối Giai đoạn 2: {e}")
        return None

    # GIAI ĐOẠN 3: Xuất bản video Reels
    publish_url = f"{base_url}/{page_id}/video_reels"
    publish_params = {
        "upload_phase": "finish",
        "access_token": access_token,
        "video_id": video_id,
        "video_state": "PUBLISHED",
        "description": caption
    }

    try:
        print("  -> Đang gửi yêu cầu xuất bản video...")
        r_pub = requests.post(publish_url, data=publish_params, timeout=60)
        pub_res = r_pub.json()

        # Meta có thể trả về {"success": true} hoặc {"id": "..."} hoặc {"video_id": "..."}
        if r_pub.status_code == 200 and (pub_res.get("success") is True or "id" in pub_res or "video_id" in pub_res):
            print(f"✅ Đăng tải Facebook Reels thành công! (Video ID: {video_id})")
            return video_id
        else:
            print(f"❌ Lỗi ở Giai đoạn 3 (Xuất bản - {r_pub.status_code}): {pub_res}")
            return None
    except Exception as e:
        print(f"❌ Lỗi kết nối Giai đoạn 3: {e}")
        return None