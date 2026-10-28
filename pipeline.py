import os
import sys
import json
import time
import asyncio
import subprocess
import requests
import textwrap
import re
from datetime import datetime
from io import BytesIO
from urllib.parse import urljoin
from pathlib import Path
from bs4 import BeautifulSoup
from PIL import Image
from dotenv import load_dotenv
from google import genai
from google.genai import types
import edge_tts
from edge_tts.exceptions import NoAudioReceived
from faster_whisper import WhisperModel
import docx

# -------------------------------------------------------------
# CẤU HÌNH THƯ MỤC & NẠP TỰ ĐỘNG TỆP .ENV
# -------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TEMP_DIR = BASE_DIR / "temp"
ASSETS_DIR = BASE_DIR / "assets"
DASHBOARD_DATA_FILE = BASE_DIR / "videos_data.json"

TEMP_DIR.mkdir(exist_ok=True)
ASSETS_DIR.mkdir(exist_ok=True)

CANVA_BANNER_PATH = ASSETS_DIR / "banner_frame.png"

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")
FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
VBEE_APP_ID = os.environ.get("VBEE_APP_ID", "")
VBEE_API_TOKEN = os.environ.get("VBEE_API_TOKEN", "")

try:
    from youtube_uploader import upload_youtube_shorts
except ImportError:
    upload_youtube_shorts = None

try:
    from facebook_uploader import upload_facebook_reels
except ImportError:
    upload_facebook_reels = None

# -------------------------------------------------------------
# 0. LƯU DỮ LIỆU DASHBOARD
# -------------------------------------------------------------
def save_to_dashboard_json(meta: dict, yt_url: str = "", fb_id: str = "", tiktok_link: str = ""):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    fb_url = f"https://facebook.com/{fb_id}" if (fb_id and not str(fb_id).startswith("http")) else (fb_id or "")

    new_entry = {
        "id": f"HN-{int(time.time())}",
        "title": meta.get("title", ""),
        "created_at": now_str,
        "category": "Chính sách",
        "voice_script": meta.get("voice_script", ""),
        "caption": meta.get("caption", ""),
        "hashtags": meta.get("hashtags", ""),
        "keywords": meta.get("keywords", []),
        "status": "published" if (yt_url or fb_url or tiktok_link) else "rendered",
        "views_yt": 0, "views_fb": 0, "views_tiktok": 0,
        "yt_link": yt_url or "", "fb_link": fb_url, "tiktok_link": tiktok_link or "",
        "last_synced": now_str
    }

    data = []
    if DASHBOARD_DATA_FILE.exists():
        try:
            with open(DASHBOARD_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []

    data = [item for item in data if item.get("title") != new_entry["title"]]
    data.insert(0, new_entry)

    with open(DASHBOARD_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"📊 Đã tự động lưu thông tin video vào {DASHBOARD_DATA_FILE.name}")

# -------------------------------------------------------------
# 1. BÓC TÁCH BÀI BÁO & HÌNH ẢNH
# -------------------------------------------------------------
def fetch_article_and_images(url: str, max_images: int = 6):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": url,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    print(f"🌐 Đang đọc bài báo: {url}")
    try:
        res = requests.get(url, headers=headers, timeout=15)
        res.raise_for_status()
    except Exception as e:
        print(f"❌ Không thể truy cập link bài báo: {e}")
        return "", []

    soup = BeautifulSoup(res.text, "html.parser")
    title_node = soup.find("h1")
    title_text = title_node.get_text(strip=True) if title_node else ""

    content_area = soup.find("div", {"class": "content-detail"}) or soup.find("article") or soup.find("body") or soup
    paragraphs = content_area.find_all("p")
    content_text = " ".join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 25])
    full_text = f"{title_text}. {content_text}".strip()

    candidate_urls = []
    og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
    if og_img and og_img.get("content"):
        candidate_urls.append(og_img.get("content").strip())

    for img in content_area.find_all("img"):
        src = img.get("data-original") or img.get("data-src") or img.get("src")
        if src and not src.startswith("data:image"):
            full_img_url = urljoin(url, src)
            if full_img_url not in candidate_urls:
                candidate_urls.append(full_img_url)

    downloaded_images = []
    seen_hashes = set()
    img_idx = 1

    for img_url in candidate_urls:
        if any(bad in img_url.lower() for bad in ["logo", "icon", "avatar", ".svg", ".gif"]):
            continue
        try:
            r = requests.get(img_url, headers=headers, timeout=12)
            if r.status_code == 200 and len(r.content) > 10000:
                with Image.open(BytesIO(r.content)) as im:
                    w, h = im.size
                    if w < 500 or h < 300:
                        continue
                    img_hash = hash(r.content[:5000])
                    if img_hash in seen_hashes:
                        continue
                    seen_hashes.add(img_hash)
                    img_path = str(TEMP_DIR / f"news_img_{img_idx}.jpg")
                    im.convert("RGB").save(img_path, "JPEG", quality=95)
                downloaded_images.append(img_path)
                img_idx += 1
                if len(downloaded_images) >= max_images:
                    break
        except Exception:
            continue

    return full_text, downloaded_images

# -------------------------------------------------------------
# 2. XỬ LÝ VĂN BẢN VÀ ẢNH TỪ WORD (.DOCX)
# -------------------------------------------------------------
def extract_docx_content_and_images(docx_path: str, max_images: int = 6):
    try:
        doc = docx.Document(docx_path)
    except Exception as e:
        print(f"❌ Lỗi đọc docx: {e}")
        return "", []

    full_text = "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
    downloaded_images = []
    img_idx = 1
    seen_hashes = set()

    for rel in doc.part.rels.values():
        if "image" in rel.target_ref:
            img_bytes = rel.target_part.blob
            img_hash = hash(img_bytes)
            if img_hash in seen_hashes:
                continue
            seen_hashes.add(img_hash)
            try:
                with Image.open(BytesIO(img_bytes)) as im:
                    w, h = im.size
                    if w < 300 or h < 200:
                        continue
                    img_path = str(TEMP_DIR / f"docx_img_{img_idx}.jpg")
                    im.convert("RGB").save(img_path, "JPEG", quality=95)
                downloaded_images.append(img_path)
                img_idx += 1
                if len(downloaded_images) >= max_images:
                    break
            except Exception:
                continue

    return full_text, downloaded_images

# -------------------------------------------------------------
# 3. TẠO KỊCH BẢN QUA GEMINI
# -------------------------------------------------------------
def generate_script(news_content: str):
    if not GEMINI_API_KEY:
        raise RuntimeError("Chưa cấu hình GEMINI_API_KEY trong file .env.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    Bạn là chiến lược gia nội dung và biên tập viên video marketing cho FS Equipment
(https://fsequipment.vn/), cửa hàng thiết bị âm thanh hướng tới người yêu nhạc,
người chơi Hi-Fi/Hi-End và người muốn nâng cấp hệ thống nghe nhạc tại gia.
Website có các nhóm như loa, ampli bán dẫn, ampli đèn, DAC/nguồn phát, đầu CD,
streamer và phụ kiện. Một số sản phẩm có thể là hàng đã qua sử dụng/thanh lý.

MỤC TIÊU: tạo video dọc 35–50 giây (TikTok, Facebook Reels, YouTube Shorts)
để xây dựng uy tín chuyên môn, tăng nhu cầu tìm hiểu sản phẩm và đưa người xem
về website. Không viết như bản tin thời sự của một tòa soạn.

NGUYÊN TẮC BẮT BUỘC:
- Chỉ khẳng định thông tin có trong dữ liệu đầu vào; không tự bịa giá, công suất,
  tình trạng hàng, xuất xứ, bảo hành, tồn kho, hiệu quả âm thanh hoặc so sánh hơn kém.
- Nếu nguồn là bài tin ngành, giải thích ngắn gọn xu hướng/công nghệ và ý nghĩa với
  người nghe nhạc; chỉ liên hệ FS Equipment tự nhiên ở câu kết, không giả vờ FS bán
  sản phẩm nếu nguồn không chứng minh điều đó.
- Nếu nguồn là trang sản phẩm, nêu nhu cầu phù hợp, điểm đáng chú ý đã được xác nhận,
  một lưu ý mua hàng/phối ghép hữu ích và lời mời xem thông tin chi tiết.
- Với hàng đã qua sử dụng, không gọi là hàng mới; nhắc người xem kiểm tra tình trạng,
  phụ kiện, nguồn gốc và điều kiện bảo hành được công bố trước khi quyết định.
- Giọng điệu am hiểu, điềm tĩnh, đáng tin, dành cho người đam mê âm thanh; tránh
  phóng đại kiểu “đỉnh nhất”, “hay nhất”, “giá sốc”, “cam kết tuyệt đối”.
- Không đọc hashtag trong lời thoại. Không viết tắt khó đọc. CTA nhẹ nhàng, rõ ràng.

Trả về JSON với đúng các trường:
1. title: tiêu đề ngắn, tối đa 9 từ, hấp dẫn nhưng không giật gân.
2. voice_script: lời thoại tiếng Việt tự nhiên khoảng 100–140 từ, mở đầu bằng một
   câu gợi vấn đề/lợi ích, có thông tin thực chất, kết thúc bằng CTA phù hợp.
3. caption: 2–3 câu, có CTA và đường dẫn nguồn/sản phẩm nếu tìm thấy trong đầu vào.
4. hashtags: 4–7 hashtag liên quan âm thanh; luôn có #FSEquipment và #AmThanhHiFi.
5. keywords: 3–6 từ khóa tiếng Anh về chủ đề/sản phẩm.
6. content_pillar: chọn một trong các giá trị: "Sản phẩm", "Kiến thức âm thanh",
   "Tin công nghệ", "Hướng dẫn phối ghép", "Chăm sóc thiết bị".
7. cta: lời kêu gọi hành động ngắn.

    Nội dung: "{news_content[:5000]}"
    Trả về duy nhất 1 JSON thuần:
    {{"title": "...", "voice_script": "...", "caption": "...", "hashtags": "...", "keywords": [...]}}
    """
    errors = []
    for model_name in ["gemini-2.5-flash", "gemini-2.0-flash"]:
        try:
            res = client.models.generate_content(
                model=model_name, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            data = json.loads(res.text)
            if not isinstance(data, dict):
                raise ValueError("Phản hồi Gemini không phải JSON object.")
            if not data.get("title") or not data.get("voice_script"):
                raise ValueError("Phản hồi Gemini thiếu title hoặc voice_script.")
            data["title"] = data["title"].upper().strip()
            return data
        except Exception as e:
            error_message = str(e).replace(GEMINI_API_KEY, "[REDACTED]")
            errors.append(f"{model_name}: {type(e).__name__}: {error_message}")
            continue
    details = " | ".join(errors)
    raise RuntimeError(f"Không thể tạo kịch bản từ Gemini. Chi tiết: {details}")

# -------------------------------------------------------------
# 4. TẠO GIỌNG ĐỌC (VBEE API & EDGE-TTS DỰ PHÒNG)
# -------------------------------------------------------------
async def generate_edge_tts_robust(text: str, output_path: str):
    """Hàm tạo giọng đọc dự phòng qua Edge-TTS nếu Vbee gặp sự cố"""
    audio_path = Path(output_path)
    try:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        voices = ("vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural")
        last_error = None
        audio_created = False

        for voice in voices:
            for attempt in range(1, 3):
                if audio_path.exists():
                    audio_path.unlink()
                communicate = edge_tts.Communicate(text, voice=voice)
                try:
                    await communicate.save(str(audio_path))
                except NoAudioReceived as e:
                    last_error = e
                    if audio_path.exists():
                        audio_path.unlink()
                    if attempt < 2:
                        print(
                            f"[WARN] Edge-TTS voice {voice} returned no audio; "
                            "retrying once."
                        )
                        await asyncio.sleep(2)
                    else:
                        print(
                            f"[WARN] Edge-TTS voice {voice} returned no audio; "
                            "trying the next voice."
                        )
                    continue

                if not audio_path.is_file() or audio_path.stat().st_size <= 1024:
                    raise RuntimeError(
                        f"Edge-TTS voice {voice} did not create a valid MP3 file."
                    )
                audio_created = True
                break

            if audio_created:
                break

        if not audio_created:
            raise RuntimeError(
                "Edge-TTS did not return audio from either Vietnamese voice."
            ) from last_error

        validation = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(audio_path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if validation.returncode != 0:
            raise RuntimeError(
                f"FFmpeg không đọc được MP3 Edge-TTS: {validation.stderr[-500:]}"
            )

        safe_output_path = str(output_path).encode("ascii", "backslashreplace").decode("ascii")
        print(f"[OK] Edge-TTS audio generated: {safe_output_path}")
    except Exception as e:
        if audio_path.exists():
            audio_path.unlink()
        safe_error = str(e).encode("ascii", "backslashreplace").decode("ascii")
        raise RuntimeError(f"Edge-TTS audio generation failed: {safe_error}") from e


async def text_to_speech(text: str, output_path: str):
    """Sử dụng Vbee Batch API tự động, nạp key từ .env an toàn 100%"""
    clean_text = re.sub(r'[\r\n]+', ' ', text).strip()

    # Lấy ID và Token từ .env (kèm fallback dự phòng)
    app_id = VBEE_APP_ID
    token = VBEE_API_TOKEN
    voice_code = "n_hanoi_male_baotrungmc_news_vc"

    # Nếu không có key thì lập tức chuyển sang Edge-TTS
    if not app_id or not token:
        print("⚠️ Không tìm thấy cấu hình Vbee trong .env. Đang chuyển sang Edge-TTS...")
        await generate_edge_tts_robust(clean_text, output_path)
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "App-Id": app_id,
        "Content-Type": "application/json"
    }

    payload = {
        "text": clean_text,
        "voiceCode": voice_code,
        "mode": "async",
        "outputFormat": "mp3",
        "bitrate": 128,
        "speed": 1.0,
        "webhookUrl": "https://httpbin.org/post"
    }

    url = "https://api.vbee.vn/v1/tts"
    print(f"🎙️ [Vbee] Đang gửi yêu cầu thu âm giọng đọc: '{voice_code}'...")

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=25)
        print(f"  -> Vbee Status Code: {r.status_code}")

        if r.status_code in [200, 201]:
            data = r.json()
            req_id = data.get("requestId") or data.get("id")

            if req_id:
                print(f"  -> Gửi thành công! Request ID = {req_id}. Đang chờ Vbee tạo audio...")
                poll_url = f"https://api.vbee.vn/v1/tts/requests/{req_id}"

                for attempt in range(1, 35):
                    time.sleep(2)
                    r_poll = requests.get(poll_url, headers=headers, timeout=12)
                    if r_poll.status_code == 200:
                        d_poll = r_poll.json()
                        status = str(d_poll.get("status")).upper()
                        print(f"     [Lần {attempt}] Trạng thái Vbee: {status}")

                        if status in ["COMPLETED", "SUCCESS"]:
                            audio_url = d_poll.get("audioLink") or d_poll.get("audio_link")
                            if audio_url:
                                print(f"  🎉 Vbee xử lý hoàn tất! Đang tải audio từ: {audio_url}")
                                audio_res = requests.get(audio_url, timeout=35)
                                # Đảm bảo nội dung tải về là dữ liệu audio thật (> 1KB) chứ không phải file text lỗi
                                if len(audio_res.content) > 1024:
                                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                                    with open(output_path, "wb") as f:
                                        f.write(audio_res.content)
                                    print(f"  ✅ Đã lưu file audio Vbee thành công vào: {output_path}")
                                    return
                                else:
                                    print("  ⚠️ Nội dung Vbee trả về không phải âm thanh hợp lệ!")
                                    break
                        elif status in ["FAILED", "ERROR"]:
                            print(f"  ❌ Vbee báo lỗi xử lý bài đọc: {d_poll}")
                            break
            else:
                print(f"  ⚠️ Không lấy được requestId từ phản hồi Vbee: {r.text}")
        else:
            print(f"  ❌ Lỗi gọi API Vbee (HTTP {r.status_code}): {r.text}")

    except Exception as e:
        print(f"  ❌ Lỗi ngoại lệ khi gọi Vbee: {e}")

    print("⚠️ [CẢNH BÁO] Không thể tạo audio qua Vbee. Đang chuyển sang Edge-TTS dự phòng...")
    await generate_edge_tts_robust(clean_text, output_path)

# -------------------------------------------------------------
# 5. TẠO PHỤ ĐỀ KARAOKE (CHỐNG LỖI F-STRING)
# -------------------------------------------------------------
def format_ass_time(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        secs += 1
        centis = 0
    return f"{hrs}:{mins:02d}:{secs:02d}.{centis:02d}"

def generate_karaoke_subtitles(audio_path: str, output_ass_path: str, script_text: str = ""):
    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(
            audio_path, language="vi", word_timestamps=True, vad_filter=False,
            initial_prompt=script_text if script_text else None
        )
        ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,Arial,42,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,70,70,480,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        for segment in segments:
            words = list(segment.words) if segment.words else []
            for i in range(0, len(words), 4):
                chunk = words[i:i + 4]
                if not chunk:
                    continue
                # Ghép chuỗi an toàn tuyệt đối, triệt tiêu hoàn toàn lỗi SyntaxError f-string
                karaoke_parts = []
                for w in chunk:
                    dur_cs = max(1, int(round((w.end - w.start) * 100)))
                    karaoke_parts.append("{\\k" + str(dur_cs) + "}" + w.word.strip())
                karaoke_txt = " ".join(karaoke_parts)
                events.append(f"Dialogue: 0,{format_ass_time(chunk[0].start)},{format_ass_time(chunk[-1].end)},Karaoke,,0,0,0,,{karaoke_txt}")

        if not events:
            return False
        with open(output_ass_path, "w", encoding="utf-8") as f:
            f.write(ass_header + "\n".join(events))
        return True
    except Exception as e:
        print(f"  ❌ Lỗi Whisper subtitles: {e}")
        return False

# -------------------------------------------------------------
# 6. HIỆU ỨNG TƯ LIỆU & THUẬT TOÁN PHÂN BỔ THỜI LƯỢNG ĐỘNG
# -------------------------------------------------------------
def probe_media_duration(media_path: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(media_path)],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0:
            duration = float(result.stdout.strip())
            if duration > 0:
                return duration
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    # Some Windows FFmpeg distributions include ffmpeg but omit ffprobe.
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(media_path)],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace"
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0

    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

def convert_image_to_video_clip(img_path: str, out_video_path: str, duration: float = 6.0, effect_idx: int = 0):
    total_frames = max(1, int(round(float(duration) * 25)))
    mode = effect_idx % 6

    if mode == 0:
        zoompan_expr = f"zoompan=z='min(zoom+0.0010,1.16)':d={total_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x680:fps=25"
    elif mode == 1:
        zoompan_expr = f"zoompan=z='if(lte(zoom,1.0),1.16,max(1.0,zoom-0.0010))':d={total_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x680:fps=25"
    elif mode == 2:
        zoompan_expr = f"zoompan=z=1.12:d={total_frames}:x='(on/{total_frames})*(iw-iw/zoom)':y='ih/2-(ih/zoom/2)':s=1080x680:fps=25"
    elif mode == 3:
        zoompan_expr = f"zoompan=z=1.12:d={total_frames}:x='(1-on/{total_frames})*(iw-iw/zoom)':y='ih/2-(ih/zoom/2)':s=1080x680:fps=25"
    elif mode == 4:
        zoompan_expr = f"zoompan=z='min(zoom+0.0008,1.14)':d={total_frames}:x='(on/{total_frames})*(iw/3)':y='(1-on/{total_frames})*(ih/3)':s=1080x680:fps=25"
    else:
        zoompan_expr = f"zoompan=z=1.12:d={total_frames}:x='iw/2-(iw/zoom/2)':y='(on/{total_frames})*(ih-ih/zoom)':s=1080x680:fps=25"

    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(img_path), "-t", f"{duration:.3f}",
        "-vf", f"scale=1080:680:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:680,{zoompan_expr},unsharp=3:3:0.8:3:3:0.0,setsar=1,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", str(out_video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not os.path.isfile(out_video_path) or os.path.getsize(out_video_path) == 0:
        raise RuntimeError(f"FFmpeg không thể tạo clip từ ảnh: {result.stderr[-800:]}")

def prepare_video_asset(input_path: str, output_path: str, duration: float) -> str:
    duration = max(0.1, float(duration))
    actual_dur = probe_media_duration(input_path)
    loop_arg = ["-stream_loop", "-1"] if (0 < actual_dur < duration) else []

    cmd = [
        "ffmpeg", "-y", *loop_arg, "-i", str(input_path), "-t", f"{duration:.3f}", "-an",
        "-vf", "scale=1080:680:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:680,unsharp=3:3:0.8:3:3:0.0,setsar=1,fps=25,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"FFmpeg không thể xử lý video tư liệu: {result.stderr[-800:]}")
    return str(output_path)

def allocate_media_timeline(media_items, audio_duration: float):
    """Thuật toán Video-First: Chiếu hết clip video, thời lượng còn lại chia đều cho ảnh tĩnh."""
    audio_duration = float(audio_duration)
    if audio_duration <= 0:
        raise ValueError("Thời lượng audio không hợp lệ; không thể phân bổ timeline tư liệu.")

    active = []
    for order, source in enumerate(media_items):
        if not source.get("enabled", True):
            continue
        item = dict(source)
        item["_order"] = order
        active.append(item)

    videos = [item for item in active if item.get("type") == "video"]
    images = [item for item in active if item.get("type", "image") == "image"]

    allocated_videos = []
    allocated_images = []
    remaining = audio_duration
    for item in videos:
        source_duration = probe_media_duration(item["path"])
        if source_duration <= 0 or remaining <= 0:
            continue
        item["duration"] = min(source_duration, remaining)
        allocated_videos.append(item)
        remaining -= item["duration"]

    if remaining > 0 and images:
        image_duration = remaining / len(images)
        for item in images:
            item["duration"] = image_duration
            allocated_images.append(item)
    elif remaining > 0 and allocated_videos:
        # With no still images, loop the last clip to cover the remaining narration.
        allocated_videos[-1]["duration"] += remaining

    return sorted(allocated_videos + allocated_images, key=lambda item: item["_order"])

def download_pixabay_broll(query: str, output_path: str):
    if not PIXABAY_API_KEY:
        return False
    url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={requests.utils.quote(query)}&video_type=film&per_page=3"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            hits = r.json().get("hits", [])
            for hit in hits:
                v_data = hit.get("videos", {}).get("medium") or hit.get("videos", {}).get("small")
                if v_data and v_data.get("url"):
                    r_video = requests.get(v_data["url"], timeout=20)
                    with open(output_path, "wb") as f:
                        f.write(r_video.content)
                    return True
    except Exception:
        pass
    return False

# -------------------------------------------------------------
# 7. DỰNG VIDEO: ĐÈ KHUNG PHÔI + DẢI XANH Y=1000 + NGÀY ĐĂNG X=150
# -------------------------------------------------------------
def render_final_video(audio_path, video_clips, output_file, title_text="", voice_script=""):
    duration = probe_media_duration(audio_path)
    if duration <= 0:
        raise RuntimeError(f"Không xác định được thời lượng audio hợp lệ: {audio_path}")
    print(f"⏱️ Thời lượng audio: {duration:.2f} giây")

    sub_file = TEMP_DIR / "subtitles.ass"
    has_sub = generate_karaoke_subtitles(audio_path, str(sub_file), script_text=voice_script)
    rel_sub_file = "temp/subtitles.ass".replace("\\", "/")

    safe_title = (title_text or "BẢN TIN THỜI SỰ").upper().strip()
    safe_title = re.sub(r"['\":\\]", "", safe_title)
    wrapped_lines = textwrap.wrap(safe_title, width=40)
    title_file = TEMP_DIR / "title.txt"
    with open(title_file, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(wrapped_lines[:2]))
    rel_title_file = "temp/title.txt".replace("\\", "/")

    date_file = TEMP_DIR / "date.txt"
    with open(date_file, "w", encoding="utf-8-sig") as f:
        f.write(f"Ngày đăng: {datetime.now().strftime('%d/%m/%Y')}")
    rel_date_file = "temp/date.txt".replace("\\", "/")

    if os.name == "nt":
        windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        font_candidates = [windows_fonts / "arialbd.ttf", windows_fonts / "arial.ttf"]
    else:
        font_candidates = [
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ]
    font_file = next((candidate for candidate in font_candidates if candidate.is_file()), None)
    if font_file is None:
        raise FileNotFoundError(
            "Không tìm thấy font để render tiêu đề. Ubuntu cần cài fonts-dejavu-core "
            "hoặc fonts-liberation."
        )
    font_file_path = str(font_file.resolve()).replace("\\", "/").replace(":", "\\:")

    valid_clips = [v for v in (video_clips or []) if os.path.exists(v) and os.path.getsize(v) > 0]
    if not valid_clips:
        fallback_clip = str(TEMP_DIR / "fallback_bg.mp4")
        fallback_result = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=#0B1528:s=1080x680:d={duration}",
             "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", fallback_clip],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if fallback_result.returncode != 0 or not os.path.isfile(fallback_clip) or os.path.getsize(fallback_clip) == 0:
            raise RuntimeError(f"FFmpeg không thể tạo video nền dự phòng: {fallback_result.stderr[-800:]}")
        valid_clips = [fallback_clip]
    video_clips = valid_clips

    def build_filter_content(include_sub: bool = True):
        lines = []
        for i in range(len(video_clips)):
            lines.append(
                f"[{i}:v]scale=1080:680:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop=1080:680,setsar=1,fps=25,format=yuv420p,setpts=PTS-STARTPTS[v{i}];"
            )
        concat_inputs = "".join([f"[v{i}]" for i in range(len(video_clips))])
        lines.append(f"{concat_inputs}concat=n={len(video_clips)}:v=1:a=0[clip_merged];")

        banner_idx = len(video_clips)
        lines.append(f"[{banner_idx}:v]scale=1080:1920,format=yuv420p[base_canvas];")
        lines.append(f"[base_canvas][clip_merged]overlay=0:245[frame_with_video];")

        # In tiêu đề vào dải xanh (Tâm y=1000)
        lines.append(
            f"[frame_with_video]drawtext=fontfile='{font_file_path}':textfile='{rel_title_file}':"
            f"x=110:y=1000-(text_h/2):fontsize=38:fontcolor=white:line_spacing=2:"
            f"shadowcolor=black@0.4:shadowx=2:shadowy=2[frame_with_title];"
        )

        # In ngày đăng ngay dưới dải xanh (dịch vào x=150, y=1110)
        if include_sub and has_sub and sub_file.exists() and sub_file.stat().st_size > 50:
            lines.append(
                f"[frame_with_title]drawtext=fontfile='{font_file_path}':textfile='{rel_date_file}':"
                f"x=180:y=1110:fontsize=30:fontcolor=0xFFD700:shadowcolor=black@0.6:shadowx=1:shadowy=1[frame_with_date];"
            )
            lines.append(f"[frame_with_date]subtitles='{rel_sub_file}'[outv]")
        else:
            lines.append(
                f"[frame_with_title]drawtext=fontfile='{font_file_path}':textfile='{rel_date_file}':"
                f"x=150:y=1110:fontsize=30:fontcolor=0xFFD700:shadowcolor=black@0.6:shadowx=1:shadowy=1[outv]"
            )
        return "".join(lines)

    filter_script_file = TEMP_DIR / "filter_script.txt"
    with open(filter_script_file, "w", encoding="utf-8") as f:
        f.write(build_filter_content(include_sub=True))
    rel_filter_script = "temp/filter_script.txt".replace("\\", "/")

    audio_idx = len(video_clips) + 1
    def get_cmd():
        cmd = ["ffmpeg", "-y"]
        for v in video_clips:
            cmd.extend(["-i", str(v)])
        cmd.extend(["-loop", "1", "-t", str(duration), "-i", str(CANVA_BANNER_PATH)])
        cmd.extend(["-i", str(audio_path)])
        cmd.extend([
            "-filter_complex_script", rel_filter_script,
            "-map", "[outv]",
            "-map", f"{audio_idx}:a",
            "-t", str(duration),
            "-af", "loudnorm=I=-14:LRA=11:TP=-1.5",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "16",
            "-b:v", "10M",
            "-maxrate", "14M",
            "-bufsize", "20M",
            "-profile:v", "high",
            "-level", "4.2",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "256k",
            str(output_file)
        ])
        return cmd

    print("🚀 Đang render video Full HD 1080p (Bitrate 10 Mbps)...")
    res = subprocess.run(get_cmd(), cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8")

    # Fallback tự động nếu libass của máy gặp lỗi đường truyền phụ đề
    if res.returncode != 0 and ("subtitles" in res.stderr or "matches no streams" in res.stderr):
        print("⚠️ Chuyển sang fallback an toàn không kèm phụ đề...")
        with open(filter_script_file, "w", encoding="utf-8") as f:
            f.write(build_filter_content(include_sub=False))
        res = subprocess.run(get_cmd(), cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8")

    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg gặp sự cố (Exit code {res.returncode}):\n{res.stderr[-800:]}")

    print(f"✅ Hoàn tất dựng video: {output_file}")

# -------------------------------------------------------------
# 8. HÀM MAIN (CHẠY TRỰC TIẾP TERMINAL)
# -------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print("Cách dùng: python pipeline.py <url_bài_báo hoặc đường_dẫn_file.docx>")
        return

    user_input = sys.argv[1].strip()
    downloaded_images = []

    if user_input.lower().endswith((".docx", ".doc")):
        news_text, downloaded_images = extract_docx_content_and_images(user_input, max_images=6)
    elif user_input.startswith(("http://", "https://")):
        news_text, downloaded_images = fetch_article_and_images(user_input, max_images=6)
    else:
        news_text = user_input

    if not news_text:
        return

    meta = generate_script(news_text)
    audio_file = str(TEMP_DIR / "voice.mp3")
    asyncio.run(text_to_speech(meta["voice_script"], audio_file))

    audio_duration = probe_media_duration(audio_file)
    media_items = [{"path": p, "enabled": True, "type": "image"} for p in downloaded_images[:6]]

    timeline = allocate_media_timeline(media_items, audio_duration)
    video_segments = []
    image_idx = 0

    for idx, item in enumerate(timeline, start=1):
        clip_path = str(TEMP_DIR / f"timeline_clip_{idx}.mp4")
        if item["type"] == "video":
            prepare_video_asset(item["path"], clip_path, item["duration"])
        else:
            convert_image_to_video_clip(item["path"], clip_path, duration=item["duration"], effect_idx=image_idx)
            image_idx += 1
        video_segments.append(clip_path)

    output_video = str(BASE_DIR / "final_news_video.mp4")
    render_final_video(
        audio_path=audio_file, video_clips=video_segments, output_file=output_video,
        title_text=meta["title"], voice_script=meta.get("voice_script", "")
    )
    save_to_dashboard_json(meta)
    print(f"🎉 HOÀN THÀNH: {output_video}")

if __name__ == "__main__":
    main()