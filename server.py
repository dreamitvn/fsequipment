import os
import json
import asyncio
import difflib
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

# Import hàm và cấu hình từ các module nội bộ
from telegram_bot import parse_single_feed, RSS_FEEDS, is_article_seen, mark_article_as_seen
from pipeline import (
    fetch_article_and_images,
    generate_script,
    text_to_speech,
    convert_image_to_video_clip,
    render_final_video,
    save_to_dashboard_json,
    TEMP_DIR,
    BASE_DIR,
    FB_PAGE_ID,
    FB_ACCESS_TOKEN
)
from sync_history import main as run_sync_all, DASHBOARD_DATA_FILE

# Import module uploader
try:
    from youtube_uploader import upload_youtube_shorts
except ImportError:
    upload_youtube_shorts = None

try:
    from facebook_uploader import upload_facebook_reels
except ImportError:
    upload_facebook_reels = None

app = FastAPI(title="Hòa Nhập Video Ops Dashboard API")

# Mốc lọc dữ liệu hệ thống mới: 15/10/2026
CUTOFF_DATE = datetime(2026, 10, 15, 0, 0, 0)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def parse_video_datetime(date_str: str):
    if not date_str:
        return None
    date_str = str(date_str).strip()
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None

def auto_publish_social(video_path: str, meta: dict):
    """Hàm tự động xuất bản lên YouTube Shorts và Facebook Reels"""
    yt_published_url = ""
    fb_published_id = ""
    caption_full = f"{meta['title']}\n\n{meta['caption']}\n\n{meta['hashtags']}"

    # 1. Đăng YouTube Shorts
    if upload_youtube_shorts:
        try:
            print("🚀 [Dashboard Auto-Post] Đang đăng video lên YouTube Shorts...")
            yt_url = upload_youtube_shorts(
                video_path=video_path,
                title=meta["title"],
                description=caption_full,
                privacy_status="public"
            )
            if yt_url:
                yt_published_url = yt_url
                print(f"  ✅ YouTube Shorts thành công: {yt_url}")
        except Exception as e:
            print(f"  ❌ YouTube đăng lỗi: {e}")
    else:
        print("  ⚠️ Module youtube_uploader chưa sẵn sàng.")

    # 2. Đăng Facebook Reels
    if upload_facebook_reels and FB_PAGE_ID and FB_ACCESS_TOKEN:
        try:
            print("🚀 [Dashboard Auto-Post] Đang đăng video lên Facebook Reels...")
            fb_id = upload_facebook_reels(
                video_path=video_path,
                caption=caption_full,
                page_id=FB_PAGE_ID,
                access_token=FB_ACCESS_TOKEN
            )
            if fb_id:
                fb_published_id = fb_id
                print(f"  ✅ Facebook Reels thành công: ID {fb_id}")
        except Exception as e:
            print(f"  ❌ Facebook đăng lỗi: {e}")
    else:
        print("  ⚠️ Thiếu cấu hình Facebook Page ID hoặc Access Token.")

    return yt_published_url, fb_published_id

class ReRenderRequest(BaseModel):
    video_id: str
    title: str
    voice_script: str

class ProcessArticleRequest(BaseModel):
    url: str

# 1. API Lấy dữ liệu Realtime (Lọc từ 15/10/2026 trở lại đây)
@app.get("/api/videos")
def get_dashboard_data():
    if DASHBOARD_DATA_FILE.exists():
        try:
            with open(DASHBOARD_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            filtered_data = []
            for item in data:
                created_dt = parse_video_datetime(item.get("created_at"))
                if created_dt is None or created_dt >= CUTOFF_DATE:
                    filtered_data.append(item)

            return JSONResponse(content=filtered_data)
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})
    return JSONResponse(content=[])

# 2. API Xem video trực tiếp trên Dashboard
@app.get("/api/video/stream/{video_id}")
def stream_video(video_id: str):
    video_file = BASE_DIR / "final_news_video.mp4"
    if video_file.exists():
        return FileResponse(str(video_file), media_type="video/mp4")
    raise HTTPException(status_code=404, detail="Chưa có tệp video thành phẩm.")

# 3. API Quét tin nóng từ 20+ luồng RSS báo chí
@app.get("/api/news/scan")
async def scan_hot_news_api(force: bool = False):
    try:
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = await loop.run_in_executor(
                pool,
                lambda: [item for feed in RSS_FEEDS for item in parse_single_feed(feed)]
            )

        if force:
            candidates = results
        else:
            candidates = [item for item in results if not is_article_seen(item["link"])]
            if not candidates:
                candidates = results

        unique_articles = []
        for item in candidates:
            if not any(difflib.SequenceMatcher(None, item["title"].lower(), s["title"].lower()).ratio() > 0.65 for s in unique_articles):
                unique_articles.append(item)

        unique_articles.sort(key=lambda x: x["score"], reverse=True)
        selected = unique_articles[:8]

        for item in selected:
            item["id"] = mark_article_as_seen(item["link"], item["title"])

        return JSONResponse(content=selected)
    except Exception as e:
        print(f"❌ Lỗi quét tin: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# 4. API Bóc tách, dựng video và TỰ ĐỘNG ĐĂNG MXH từ Dashboard
def task_process_and_render_video(url: str):
    print(f"🚀 Bắt đầu quy trình tự động làm video từ: {url}")
    news_text, images = fetch_article_and_images(url, max_images=6)
    if not news_text:
        print("❌ Không lấy được nội dung bài báo.")
        return

    meta = generate_script(news_text)
    audio_file = str(TEMP_DIR / "voice.mp3")
    asyncio.run(text_to_speech(meta["voice_script"], audio_file))

    video_segments = []
    for idx, img_path in enumerate(images[:6], start=1):
        clip_path = str(TEMP_DIR / f"img_clip_{idx}.mp4")
        convert_image_to_video_clip(img_path, clip_path, duration=4, effect_idx=idx - 1)
        video_segments.append(clip_path)

    output_video = str(BASE_DIR / "final_news_video.mp4")
    render_final_video(
        audio_path=audio_file,
        video_clips=video_segments,
        output_file=output_video,
        title_text=meta["title"],
        voice_script=meta.get("voice_script", "")
    )

    # Tự động xuất bản lên MXH ngay sau khi render xong
    yt_url, fb_id = auto_publish_social(output_video, meta)

    # Lưu thông tin video và link đã đăng vào videos_data.json
    save_to_dashboard_json(meta, yt_url=yt_url, fb_id=fb_id)
    print("🎉 Hoàn tất quy trình: Dựng xong và đã đăng lên MXH thành công!")

@app.post("/api/news/create-video")
def create_video_from_article(payload: ProcessArticleRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(task_process_and_render_video, payload.url)
    return {"message": "Đang bóc tách bài báo, dựng video và tự động đăng lên YouTube/Facebook trong nền..."}

# 5. API Re-render video và cập nhật lên MXH nếu cần
def process_rerender(payload: ReRenderRequest):
    if DASHBOARD_DATA_FILE.exists():
        with open(DASHBOARD_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for v in data:
            if v.get("id") == payload.video_id:
                v["title"] = payload.title.upper()
                v["voice_script"] = payload.voice_script
                break
        with open(DASHBOARD_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    subprocess.run(
        [sys.executable, str(BASE_DIR / "pipeline.py"), payload.voice_script],
        check=True,
        cwd=str(BASE_DIR)
    )

@app.post("/api/video/rerender")
def trigger_rerender(payload: ReRenderRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_rerender, payload)
    return {"message": "Đã tiếp nhận yêu cầu dựng lại video trong nền."}

# 6. API Đồng bộ views mạng xã hội
@app.post("/api/sync-realtime")
def trigger_sync(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_sync_all)
    return {"message": "Đang đồng bộ số liệu trong nền..."}

# 7. Tự động đồng bộ định kỳ mỗi 15 phút
async def auto_refresh_views():
    while True:
        try:
            print("🔄 [Tự động] Đang cập nhật số lượt xem realtime từ các nền tảng...")
            run_sync_all()
        except Exception as err:
            print(f"⚠️ Lỗi tự động đồng bộ: {err}")
        await asyncio.sleep(900)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_refresh_views())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.environ.get("API_HOST", "127.0.0.1"),
        port=int(os.environ.get("API_PORT", "8000"))
    )