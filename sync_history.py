import os
import json
import time
import requests
import subprocess
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DASHBOARD_DATA_FILE = BASE_DIR / "videos_data.json"

# Cấu hình kênh
TIKTOK_CHANNEL_URL = "https://www.tiktok.com/@hoanhap.vn"  # Thay đúng username TikTok của Tạp chí
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")
FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN", "")

# -------------------------------------------------------------
# 1. QUÉT YOUTUBE CHUYÊN SÂU
# -------------------------------------------------------------
def get_youtube_videos():
    videos = []
    try:
        from youtube_uploader import get_authenticated_service
        youtube = get_authenticated_service()

        print("🔴 Đang truy vấn kênh YouTube...")
        # Lấy thông tin kênh (dùng mine=True)
        res_channels = youtube.channels().list(mine=True, part="contentDetails,statistics,id").execute()

        items = res_channels.get("items", [])
        if not items:
            print("⚠️ Không tìm thấy kênh nào từ phiên đăng nhập hiện tại. Hãy kiểm tra lại file token OAuth!")
            return videos

        channel = items[0]
        channel_id = channel["id"]
        uploads_playlist_id = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        print(f"  -> Đã nhận diện Kênh ID: {channel_id} | Playlist tải lên: {uploads_playlist_id}")

        all_video_ids = []
        next_page_token = None

        while True:
            res_playlist = youtube.playlistItems().list(
                playlistId=uploads_playlist_id,
                part="contentDetails",
                maxResults=50,
                pageToken=next_page_token
            ).execute()

            for item in res_playlist.get("items", []):
                vid = item["contentDetails"]["videoId"]
                if vid not in all_video_ids:
                    all_video_ids.append(vid)

            next_page_token = res_playlist.get("nextPageToken")
            print(f"  -> Đang quét YouTube: {len(all_video_ids)} video...")
            if not next_page_token:
                break

        # Lấy số liệu chi tiết (View, Title, Description)
        for i in range(0, len(all_video_ids), 50):
            chunk = all_video_ids[i:i+50]
            res_v = youtube.videos().list(id=",".join(chunk), part="snippet,statistics").execute()
            for v in res_v.get("items", []):
                snip = v.get("snippet", {})
                stats = v.get("statistics", {})
                pub_at = snip.get("publishedAt", "")[:16].replace("T", " ")
                views = int(stats.get("viewCount", 0))

                videos.append({
                    "id": f"YT-{v['id']}",
                    "title": snip.get("title", "").replace(" #Shorts", "").replace("#Shorts", "").strip(),
                    "created_at": pub_at,
                    "category": "Chính sách",
                    "voice_script": snip.get("description", "")[:250],
                    "caption": snip.get("description", ""),
                    "hashtags": " ".join([f"#{t}" for t in snip.get("tags", [])]) if snip.get("tags") else "#hoanhap",
                    "status": "published",
                    "views_yt": views,
                    "views_fb": 0,
                    "views_tiktok": 0,
                    "yt_link": f"https://youtube.com/shorts/{v['id']}",
                    "fb_link": "",
                    "tiktok_link": "",
                    "last_synced": datetime.now().strftime("%Y-%m-%d %H:%M")
                })
        print(f"✅ Đã quét thành công {len(videos)} video từ YouTube.")
    except Exception as e:
        print(f"❌ Lỗi khi đồng bộ YouTube: {e}")
    return videos

# -------------------------------------------------------------
# 2. QUÉT TIKTOK BẰNG YT-DLP (KHÔNG CẦN API TOKEN)
# -------------------------------------------------------------
def get_tiktok_videos():
    videos = []
    print(f"\n🎵 Đang quét video từ kênh TikTok: {TIKTOK_CHANNEL_URL}...")
    try:
        # Sử dụng yt-dlp để trích xuất JSON toàn bộ video trên profile TikTok
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--dump-single-json",
            "--playlist-end", "50",  # Lấy tối đa 50 video gần nhất
            TIKTOK_CHANNEL_URL
        ]
        ret = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if ret.returncode == 0 and ret.stdout:
            data = json.loads(ret.stdout)
            entries = data.get("entries", [])
            print(f"  -> Tìm thấy {len(entries)} video trên kênh TikTok.")

            for item in entries:
                v_id = item.get("id")
                title = item.get("title") or "Video TikTok Hòa Nhập"
                url = item.get("url") or f"https://www.tiktok.com/@{data.get('uploader_id')}/video/{v_id}"
                views = int(item.get("view_count") or 0)

                # Chuyển đổi timestamp nếu có
                timestamp = item.get("timestamp")
                if timestamp:
                    dt = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
                else:
                    dt = datetime.now().strftime("%Y-%m-%d %H:%M")

                videos.append({
                    "id": f"TT-{v_id}",
                    "title": title[:100],
                    "created_at": dt,
                    "category": "Chính sách",
                    "voice_script": title,
                    "caption": title,
                    "hashtags": "#hoanhap #tiktok",
                    "status": "published",
                    "views_yt": 0,
                    "views_fb": 0,
                    "views_tiktok": views,
                    "yt_link": "",
                    "fb_link": "",
                    "tiktok_link": url,
                    "last_synced": datetime.now().strftime("%Y-%m-%d %H:%M")
                })
            print(f"✅ Đã quét thành công {len(videos)} video TikTok.")
        else:
            print(f"⚠️ Không thể trích xuất dữ liệu TikTok: {ret.stderr[:200]}")
    except Exception as e:
        print(f"⚠️ Lỗi quét TikTok: {e}")
    return videos

# -------------------------------------------------------------
# 3. QUÉT FACEBOOK REELS
# -------------------------------------------------------------
def get_facebook_reels():
    videos = []
    if not FB_PAGE_ID or not FB_ACCESS_TOKEN:
        return videos

    print(f"\n🔵 Đang quét video từ Fanpage Facebook...")
    url = f"https://graph.facebook.com/v20.0/{FB_PAGE_ID}/videos"
    params = {
        "fields": "id,description,created_time,views,permalink_url",
        "access_token": FB_ACCESS_TOKEN,
        "limit": 50
    }
    try:
        while url:
            r = requests.get(url, params=params, timeout=20)
            data = r.json()
            for item in data.get("data", []):
                desc = item.get("description", "Video không tiêu đề")
                title = desc.split("\n")[0][:100]
                created_time = item.get("created_time", "")[:16].replace("T", " ")
                views = int(item.get("views", 0))

                videos.append({
                    "id": f"FB-{item['id']}",
                    "title": title,
                    "created_at": created_time,
                    "category": "Chính sách",
                    "voice_script": desc[:250],
                    "caption": desc,
                    "hashtags": "#thoisu #hoanhap",
                    "status": "published",
                    "views_yt": 0,
                    "views_fb": views,
                    "views_tiktok": 0,
                    "yt_link": "",
                    "fb_link": f"https://facebook.com/{item['id']}",
                    "tiktok_link": "",
                    "last_synced": datetime.now().strftime("%Y-%m-%d %H:%M")
                })
            url = data.get("paging", {}).get("next")
            params = {}
        print(f"✅ Đã quét thành công {len(videos)} video Facebook.")
    except Exception as e:
        print(f"⚠️ Lỗi quét Facebook: {e}")
    return videos

# -------------------------------------------------------------
# 4. HỢP NHẤT TOÀN DIỆN VÀO FILE VIDEOS_DATA.JSON
# -------------------------------------------------------------
def main():
    existing_data = []
    if DASHBOARD_DATA_FILE.exists():
        try:
            with open(DASHBOARD_DATA_FILE, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception:
            existing_data = []

    yt_videos = get_youtube_videos()
    tt_videos = get_tiktok_videos()
    fb_videos = get_facebook_reels()

    # Bảng ánh xạ video duy nhất
    video_map = {}

    # Nạp video cũ
    for v in existing_data:
        key = v.get("yt_link") or v.get("tiktok_link") or v.get("fb_link") or v.get("id")
        video_map[key] = v

    # Hợp nhất YouTube
    for ytv in yt_videos:
        key = ytv["yt_link"]
        if key in video_map:
            video_map[key]["views_yt"] = ytv["views_yt"]
            video_map[key]["last_synced"] = ytv["last_synced"]
            video_map[key]["status"] = "published"
        else:
            video_map[key] = ytv

    # Hợp nhất TikTok
    for ttv in tt_videos:
        key = ttv["tiktok_link"]
        if key in video_map:
            video_map[key]["views_tiktok"] = ttv["views_tiktok"]
        else:
            # Thử tìm xem có video nào cùng tiêu đề không để gán link TikTok vào
            matched = False
            for k, val in video_map.items():
                if val.get("title", "").strip().lower() == ttv["title"].strip().lower():
                    val["tiktok_link"] = ttv["tiktok_link"]
                    val["views_tiktok"] = ttv["views_tiktok"]
                    matched = True
                    break
            if not matched:
                video_map[key] = ttv

    # Hợp nhất Facebook
    for fbv in fb_videos:
        key = fbv["fb_link"]
        if key in video_map:
            video_map[key]["views_fb"] = fbv["views_fb"]
        else:
            video_map[key] = fbv

    final_list = list(video_map.values())
    final_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    with open(DASHBOARD_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)

    print(f"\n==========================================")
    print(f"🎉 ĐỒNG BỘ THÀNH CÔNG TỔNG CỘNG: {len(final_list)} VIDEO (YouTube, Facebook, TikTok)!")
    print(f"📁 Tệp dữ liệu: {DASHBOARD_DATA_FILE}")

if __name__ == "__main__":
    main()