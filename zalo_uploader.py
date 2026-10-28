import os
import requests
import logging

ZALO_OA_ACCESS_TOKEN = os.environ.get("ZALO_OA_ACCESS_TOKEN", "")

def upload_zalo_video(video_path: str, access_token: str = None) -> str:
    """Tải tệp video lên kho media của Zalo OA và trả về video_id"""
    token = access_token or ZALO_OA_ACCESS_TOKEN
    if not token:
        logging.error("Chưa cấu hình ZALO_OA_ACCESS_TOKEN.")
        return ""

    url = "https://openapi.zalo.me/v2.0/oa/upload/video"
    headers = {"access_token": token}

    try:
        with open(video_path, "rb") as f:
            files = {"file": f}
            res = requests.post(url, headers=headers, files=files, timeout=60)
            data = res.json()
            if data.get("error") == 0:
                video_id = data.get("data", {}).get("video_id")
                logging.info(f"Upload Zalo OA thành công, video_id: {video_id}")
                return video_id
            else:
                logging.error(f"Lỗi upload video Zalo: {data}")
                return ""
    except Exception as e:
        logging.error(f"Ngoại lệ khi upload video Zalo: {e}")
        return ""

def create_zalo_video_article(video_id: str, title: str, description: str, access_token: str = None) -> str:
    """Tạo bài viết video trên Zalo OA"""
    token = access_token or ZALO_OA_ACCESS_TOKEN
    if not token or not video_id:
        return ""

    url = "https://openapi.zalo.me/v2.0/oa/article/create"
    headers = {
        "access_token": token,
        "Content-Type": "application/json"
    }

    payload = {
        "type": "video",
        "title": title[:150],
        "description": description[:300],
        "video_id": video_id,
        "comment": "show"
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        data = res.json()
        if data.get("error") == 0:
            article_id = data.get("data", {}).get("article_id")
            logging.info(f"Đăng bài Zalo OA thành công, article_id: {article_id}")
            return article_id
        else:
            logging.error(f"Lỗi tạo bài viết Zalo OA: {data}")
            return ""
    except Exception as e:
        logging.error(f"Ngoại lệ khi tạo bài viết Zalo OA: {e}")
        return ""

def publish_to_zalo_oa(video_path: str, title: str, description: str) -> str:
    """Hàm wrapper tiện ích thực hiện trọn gói cả 2 bước"""
    video_id = upload_zalo_video(video_path)
    if not video_id:
        return ""
    article_id = create_zalo_video_article(video_id, title, description)
    return article_id