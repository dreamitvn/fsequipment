import os
import html
import shutil
import asyncio
import logging
import sqlite3
import difflib
import re
import time
import requests
from urllib.parse import urljoin, urlparse
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from pipeline import (
    fetch_article_and_images,
    extract_docx_content_and_images,
    generate_script,
    text_to_speech,
    convert_image_to_video_clip,
    download_pixabay_broll,
    render_final_video,
    probe_media_duration,
    prepare_video_asset,
    allocate_media_timeline,
    save_to_dashboard_json,
    TEMP_DIR,
    BASE_DIR,
    PIXABAY_API_KEY,
    FB_PAGE_ID,
    FB_ACCESS_TOKEN
)

try:
    from youtube_uploader import upload_youtube_shorts
except ImportError:
    upload_youtube_shorts = None

try:
    from facebook_uploader import upload_facebook_reels
except ImportError:
    upload_facebook_reels = None

# -------------------------------------------------------------
# CẤU HÌNH TOKEN & PHÂN QUYỀN (NẠP .ENV)
# -------------------------------------------------------------
load_dotenv(BASE_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_IDS = {
    chat_id.strip()
    for chat_id in os.environ.get("ALLOWED_CHAT_ID", "").split(",")
    if chat_id.strip()
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

USER_SESSIONS = {}
USER_STATES = {}

# -------------------------------------------------------------
# CƠ SỞ DỮ LIỆU SQLITE
# -------------------------------------------------------------
DB_PATH = BASE_DIR / "scraped_news.db"

# Nguồn nội dung phù hợp với marketing thiết bị âm thanh.
# Sản phẩm FS Equipment được quét trực tiếp từ website; RSS chỉ dùng cho tin ngành.
FS_EQUIPMENT_HOME = "https://fsequipment.vn/"
RSS_FEEDS = [
    # RSS/Atom feeds đã biết
    {
        "source": "Sound & Vision – tin công nghệ/thiết bị âm thanh",
        "url": "https://www.soundandvision.com/rss.xml",
        "type": "rss",
    },
    {
        "source": "The Absolute Sound – Hi-Fi/Hi-End",
        "url": "https://www.theabsolutesound.com/articles/feed/",
        "type": "rss",
    },
    # Các website dưới đây được quét trang tin/trang chủ vì chưa xác nhận được RSS công khai ổn định.
    # parse_single_feed() sẽ xử lý chúng như trang HTML, không đưa URL trang chủ cho feedparser.
    {"source": "TOA Việt Nam – tin tức và kiến thức âm thanh", "url": "https://www.toa-vn.com/news", "type": "html"},
    {"source": "Techland Audio – tin tức Hi-Fi/Hi-End", "url": "https://techlandaudio.com.vn/", "type": "html"},
    {"source": "Châu Audio – thiết bị âm thanh Hi-Fi/Hi-End", "url": "https://chauaudio.com/", "type": "html"},
    {"source": "T3B – âm thanh chuyên nghiệp và thiết bị mới", "url": "https://t3b.vn/", "type": "html"},
    {"source": "Stereo.vn – tin tức và trải nghiệm âm thanh", "url": "https://stereo.vn/", "type": "html"},
    {"source": "Mixonline – Technology / News & Products", "url": "https://www.mixonline.com/technology/news-products", "type": "html"},
    {"source": "ProSoundWeb – tin tức âm thanh chuyên nghiệp", "url": "https://www.prosoundweb.com/", "type": "html"},
]

AUDIO_KEYWORDS = [
    "hi-fi", "hifi", "hi-end", "audiophile", "loudspeaker", "speaker",
    "amplifier", "integrated amplifier", "tube amplifier", "dac",
    "streamer", "turntable", "vinyl", "audio", "sound system",
    "headphone", "preamp", "power amplifier", "cd player",
    "loa", "ampli", "amply", "âm thanh", "đầu giải mã", "đầu cd",
    "mâm đĩa than", "phối ghép", "bowers", "wilkins", "marantz",
    "mcintosh", "linn", "unison research", "sound warrior",
]


def init_news_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT UNIQUE,
            title TEXT,
            published_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def is_article_seen(link: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_articles WHERE link = ?", (link,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def mark_article_as_seen(link: str, title: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO seen_articles (link, title, published_at)
        VALUES (?, ?, ?)
    """, (link, title, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    article_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return article_id

def get_link_by_id(article_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT link FROM seen_articles WHERE id = ?", (article_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

# -------------------------------------------------------------
# DỌN DẸP TỆP TIN TẠM
# -------------------------------------------------------------
def cleanup_temp_files():
    deleted_count = 0
    total_size = 0
    if TEMP_DIR.exists():
        for item in TEMP_DIR.iterdir():
            try:
                if item.is_file():
                    total_size += item.stat().st_size
                    item.unlink()
                    deleted_count += 1
                elif item.is_dir():
                    for f in item.glob("**/*"):
                        if f.is_file():
                            total_size += f.stat().st_size
                    shutil.rmtree(item)
                    deleted_count += 1
            except Exception as e:
                logging.error(f"Không thể xóa {item}: {e}")
    size_mb = round(total_size / (1024 * 1024), 2)
    return deleted_count, size_mb

async def daily_cleanup_scheduler():
    while True:
        now = datetime.now()
        next_midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        await asyncio.sleep((next_midnight - now).total_seconds())
        try:
            count, mb = cleanup_temp_files()
            app = getattr(daily_cleanup_scheduler, "app", None)
            if app:
                for chat_id in ALLOWED_CHAT_IDS:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🧹 <b>[HỆ THỐNG] DỌN DẸP ĐỊNH KỲ 00:00</b>\n"
                            f"• Đã xóa: <b>{count} tệp tin & thư mục tạm</b>\n"
                            f"• Giải phóng: <b>{mb} MB</b>"
                        ),
                        parse_mode="HTML"
                    )
        except Exception as e:
            logging.error(f"Lỗi dọn dẹp định kỳ: {e}")

# -------------------------------------------------------------
# MENU BÀN PHÍM & GIAO DIỆN QUẢN LÝ TƯ LIỆU
# -------------------------------------------------------------
def get_main_menu_keyboard():
    keyboard = [
        [KeyboardButton("🔗 Dán link sản phẩm/bài viết"), KeyboardButton("✍️ Nhập nội dung tự do")],
        [KeyboardButton("📄 Gửi file Word (.docx)"), KeyboardButton("🎧 Tìm nội dung âm thanh mới")],
        [KeyboardButton("🧹 Dọn sạch tệp tạm (Temp)")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def build_media_management_keyboard(media_status):
    keyboard = []
    for idx, item in enumerate(media_status):
        path, is_selected, media_type = item
        icon = "🎬" if media_type == "video" else "🖼️"
        status_icon = "✅" if is_selected else "❌"
        row = [
            InlineKeyboardButton(f"{icon} #{idx + 1} {status_icon}", callback_data=f"toggle_{idx}")
        ]
        if idx > 0:
            row.append(InlineKeyboardButton("⬆️ Lên", callback_data=f"move_up_{idx}"))
        if idx < len(media_status) - 1:
            row.append(InlineKeyboardButton("⬇️ Xuống", callback_data=f"move_down_{idx}"))
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton("✏️ Sửa lời đọc (Voice)", callback_data="edit_voice_script"),
        InlineKeyboardButton("🏷️ Sửa tiêu đề Banner", callback_data="edit_title")
    ])
    keyboard.append([
        InlineKeyboardButton("🚀 Dựng & Đăng MXH", callback_data="render_and_publish"),
        InlineKeyboardButton("🎬 Chỉ Dựng Video", callback_data="render_only")
    ])
    keyboard.append([InlineKeyboardButton("❌ Hủy bỏ phiên này", callback_data="cancel_action")])
    return InlineKeyboardMarkup(keyboard)

def format_preview_text(meta: dict) -> str:
    safe_title = html.escape(str(meta.get('title', '')))
    safe_voice = html.escape(str(meta.get('voice_script', '')))
    return (
        "🎧 <b>KỊCH BẢN VIDEO FS EQUIPMENT ĐÃ SẴN SÀNG!</b>\n\n"
        f"🏷️ <b>Tiêu đề banner:</b> <code>{safe_title}</code>\n\n"
        f"🎙️ <b>Lời đọc phát thanh:</b>\n<i>\"{safe_voice}\"</i>\n\n"
        "🖼️🎬 <b>QUẢN LÝ TƯ LIỆU:</b>\n"
        "• Bấm vào nút tư liệu để <b>Bật ✅ / Tắt ❌</b>.\n"
        "• Dùng <b>⬆️ Lên / ⬇️ Xuống</b> để đảo thứ tự hiển thị.\n"
        "• Gửi thêm ảnh hoặc video (.mp4, .mov...) vào chat bất cứ lúc nào!"
    )

# -------------------------------------------------------------
# KHỞI TẠO PHIÊN
# -------------------------------------------------------------
async def init_session_with_content(content_text: str, images: list, source_label: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await context.bot.send_message(
        chat_id=chat_id, text="🧠 Đang phân tích nội dung và tạo kịch bản video AI... Vui lòng đợi!"
    )
    try:
        script_input = f"Nguồn URL: {source_label}\n\n{content_text}" if source_label.startswith(("http://", "https://")) else content_text
        meta = generate_script(script_input)
        media_status = [[img, True, 'image'] for img in images]

        USER_SESSIONS[chat_id] = {
            "meta": meta,
            "media_status": media_status,
            "url": source_label,
            "control_msg_id": None
        }
        await status_msg.delete()

        if any(item[2] == "image" for item in media_status):
            media_group = [
                InputMediaPhoto(media=open(img_path, "rb"), caption=f"Ảnh #{idx + 1}")
                for idx, (img_path, _, m_type) in enumerate(media_status) if m_type == "image"
            ]
            await context.bot.send_media_group(chat_id=chat_id, media=media_group)

        control_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=format_preview_text(meta),
            parse_mode="HTML",
            reply_markup=build_media_management_keyboard(media_status)
        )
        USER_SESSIONS[chat_id]["control_msg_id"] = control_msg.message_id
    except Exception as e:
        logging.error(f"Lỗi khởi tạo kịch bản: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Xảy ra lỗi khi tạo kịch bản: {e}")

# -------------------------------------------------------------
# QUÉT TIN RSS TIÊU ĐIỂM
# -------------------------------------------------------------
def parse_single_feed(feed_info):
    """Đọc RSS hoặc quét liên kết bài viết từ website không có RSS đã xác minh."""
    items = []
    source_url = feed_info.get("url", "")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        if feed_info.get("type", "rss") == "html":
            response = requests.get(source_url, headers=headers, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            base_domain = urlparse(source_url).netloc.lower().removeprefix("www.")
            seen_links = set()
            candidates = []

            # Ưu tiên các thẻ tiêu đề/bài viết; sau đó dùng liên kết có anchor text đủ rõ.
            for heading in soup.select("h1 a[href], h2 a[href], h3 a[href], article a[href], .post a[href], .entry a[href]"):
                candidates.append(heading)
            if not candidates:
                candidates = soup.select("a[href]")

            for anchor in candidates:
                title = " ".join((anchor.get("title") or anchor.get_text(" ", strip=True) or "").split())
                href = (anchor.get("href") or "").strip()
                link = urljoin(source_url, href).split("#", 1)[0]
                parsed = urlparse(link)
                domain = parsed.netloc.lower().removeprefix("www.")
                if not title or len(title) < 12 or domain != base_domain:
                    continue
                if link.rstrip("/") == source_url.rstrip("/") or link in seen_links:
                    continue
                if parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".svg", ".pdf", ".mp4", ".zip")):
                    continue
                if any(part in parsed.path.lower() for part in ("/tag/", "/category/", "/author/", "/cart", "/checkout", "/account", "/search")):
                    continue
                seen_links.add(link)
                searchable = title.lower()
                score = sum(2 for kw in AUDIO_KEYWORDS if kw in searchable)
                # Đây đều là nguồn chuyên ngành âm thanh; vẫn loại bỏ các liên kết điều hướng chung.
                if score == 0 and len(title.split()) < 4:
                    continue
                items.append({
                    "source": feed_info["source"],
                    "title": title,
                    "link": link,
                    "summary": "Nguồn: " + feed_info["source"],
                    "score": max(score, 1),
                    "is_product": False,
                    "is_hot": score >= 4,
                })
                if len(items) >= 10:
                    break
        else:
            parsed = feedparser.parse(source_url)
            for entry in parsed.entries[:10]:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
                if not link or not title:
                    continue
                searchable = f"{title} {summary}".lower()
                score = sum(2 for kw in AUDIO_KEYWORDS if kw in searchable)
                if score == 0:
                    continue
                items.append({
                    "source": feed_info["source"],
                    "title": title,
                    "link": link,
                    "summary": summary[:240] + ("..." if len(summary) > 240 else ""),
                    "score": score,
                    "is_product": False,
                    "is_hot": score >= 4,
                })
    except Exception as exc:
        logging.warning("Không đọc được nguồn %s (%s): %s", feed_info.get("source"), source_url, exc)
    return items


def fetch_fs_equipment_products(max_items=30):
    """Quét các URL sản phẩm đang được liên kết công khai từ trang chủ FS Equipment."""
    items = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        response = requests.get(FS_EQUIPMENT_HOME, headers=headers, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        seen_urls = set()

        for anchor in soup.select('a[href]'):
            href = (anchor.get("href") or "").strip()
            product_url = urljoin(FS_EQUIPMENT_HOME, href)
            parsed_url = urlparse(product_url)
            if parsed_url.netloc.lower() not in {"fsequipment.vn", "www.fsequipment.vn"}:
                continue
            if "/shops/" not in parsed_url.path.lower() or not parsed_url.path.lower().endswith(".html"):
                continue
            product_url = product_url.split("#", 1)[0]
            if product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            image = anchor.find("img")
            image_title = (image.get("alt") or "").strip() if image else ""
            title_attr = (anchor.get("title") or "").strip()
            anchor_text = anchor.get_text(" ", strip=True)
            # Tránh lấy nhầm chữ trên nút như "Đặt hàng" làm tên sản phẩm.
            candidates = [title_attr, image_title, anchor_text]
            title = next(
                (candidate for candidate in candidates
                 if len(candidate) >= 8
                 and candidate.lower() not in {"đặt hàng", "mua ngay", "yêu thích", "xem thêm"}),
                "",
            )
            if not title:
                continue

            items.append({
                "source": "Sản phẩm trên website FS Equipment",
                "title": title[:220],
                "link": product_url,
                "summary": "Trang sản phẩm chính thức trên fsequipment.vn. Mở trang để kiểm tra mô tả, tình trạng, giá và thông tin hiện hành trước khi dựng video.",
                "score": 100,
                "is_product": True,
                "is_hot": False,
            })
            if len(items) >= max_items:
                break
    except Exception as exc:
        logging.warning("Không quét được sản phẩm FS Equipment: %s", exc)
    return items


async def scan_and_send_hot_news(bot, target_chat_id: str, is_manual=False):
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=8) as pool:
        product_future = loop.run_in_executor(pool, fetch_fs_equipment_products, 30)
        feed_future = loop.run_in_executor(
            pool, lambda: [item for feed in RSS_FEEDS for item in parse_single_feed(feed)]
        )
        products, feed_items = await asyncio.gather(product_future, feed_future)

    candidates = products + feed_items
    unread_candidates = [item for item in candidates if not is_article_seen(item["link"])]
    unique_items = []
    for item in unread_candidates:
        duplicate = any(
            difflib.SequenceMatcher(None, item["title"].lower(), old["title"].lower()).ratio() > 0.78
            for old in unique_items
        )
        if not duplicate:
            unique_items.append(item)

    unique_items.sort(key=lambda item: item["score"], reverse=True)
    selected_items = unique_items[:8]

    if not selected_items:
        if is_manual:
            await bot.send_message(
                chat_id=target_chat_id,
                text="ℹ️ Chưa phát hiện sản phẩm mới trên FS Equipment hoặc tin ngành âm thanh phù hợp.",
            )
        return

    for item in selected_items:
        item["id"] = mark_article_as_seen(item["link"], item["title"])

    now_str = datetime.now().strftime("%H:%M - %d/%m/%Y")
    await bot.send_message(
        chat_id=target_chat_id,
        text=(
            f"🎧 <b>NỘI DUNG MARKETING FS EQUIPMENT ({now_str})</b>\\n"
            f"<i>Đã tìm thấy {len(selected_items)} nội dung chưa xử lý:</i>"
        ),
        parse_mode="HTML",
    )

    for item in selected_items:
        if item.get("is_product"):
            tag = "🛒 [SẢN PHẨM FS EQUIPMENT]"
            action = "🎬 Tạo video sản phẩm"
        else:
            tag = "🎵 [TIN CÔNG NGHỆ ÂM THANH]"
            action = "🎬 Tạo video kiến thức/tin ngành"

        keyboard = [[
            InlineKeyboardButton(action, callback_data=f"news_pick_{item['id']}"),
            InlineKeyboardButton("🌐 Mở nguồn", url=item["link"]),
        ]]
        await bot.send_message(
            chat_id=target_chat_id,
            text=(
                f"<b>{tag} {html.escape(item['title'])}</b>\\n"
                f"🏷 Nguồn: <i>{html.escape(item['source'])}</i>\\n"
                f"📝 {html.escape(item['summary'])}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        await asyncio.sleep(0.4)

async def periodic_news_scanner(app):
    while True:
        try:
            for chat_id in ALLOWED_CHAT_IDS:
                await scan_and_send_hot_news(app.bot, chat_id, is_manual=False)
        except Exception as e:
            logging.error(f"Lỗi quét tin tự động: {e}")
        await asyncio.sleep(2 * 3600)

# -------------------------------------------------------------
# XỬ LÝ LỆNH & TIN NHẮN TỪ NGƯỜI DÙNG
# -------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    if ALLOWED_CHAT_IDS and user_id not in ALLOWED_CHAT_IDS:
        await update.message.reply_text("⛔ Bạn không có quyền truy cập.")
        return

    msg = (
        "👋 Chào mừng bạn đến với <b>Hệ thống video marketing FS Equipment</b>!\n\n"
        "👇 <b>Chọn một phương thức bên dưới bàn phím để bắt đầu:</b>\n"
        "• <b>🔗 Dán link sản phẩm/bài viết:</b> Lấy nội dung từ trang sản phẩm hoặc bài viết ngành âm thanh.\n"
        "• <b>✍️ Nhập nội dung tự do:</b> Dán nội dung và gửi ảnh/video tư liệu.\n"
        "• <b>📄 Gửi file Word (.docx):</b> Bóc tách văn bản và toàn bộ ảnh nhúng.\n"
        "• <b>🎧 Tìm nội dung âm thanh mới:</b> Quét sản phẩm trên FS Equipment và tin ngành âm thanh.\n"
        "• <b>🧹 Dọn sạch tệp tạm (Temp):</b> Giải phóng dung lượng ổ cứng."
    )
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=get_main_menu_keyboard())

async def clean_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    if ALLOWED_CHAT_IDS and user_id not in ALLOWED_CHAT_IDS:
        return
    count, mb = cleanup_temp_files()
    await update.message.reply_text(
        f"🧹 <b>ĐÃ DỌN DẸP XONG!</b>\n• Số tệp/thư mục: <b>{count}</b>\n• Giải phóng: <b>{mb} MB</b>",
        parse_mode="HTML"
    )

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if ALLOWED_CHAT_IDS and str(chat_id) not in ALLOWED_CHAT_IDS:
        return

    text = update.message.text.strip()
    current_state = USER_STATES.get(chat_id)
    session = USER_SESSIONS.get(chat_id)

    if text == "🧹 Dọn sạch tệp tạm (Temp)":
        count, mb = cleanup_temp_files()
        await update.message.reply_text(f"🧹 <b>Đã dọn sạch:</b> {count} tệp ({mb} MB)", parse_mode="HTML")
        return

    if text == "🔗 Dán link sản phẩm/bài viết":
        USER_STATES[chat_id] = "WAITING_URL"
        await update.message.reply_text("👉 Hãy gửi đường link bài báo vào đây:")
        return

    if text == "✍️ Nhập nội dung tự do":
        USER_STATES[chat_id] = "WAITING_RAW_CONTENT"
        await update.message.reply_text("📝 Hãy gửi hoặc dán nội dung bài viết vào đây:")
        return

    if text == "📄 Gửi file Word (.docx)":
        USER_STATES[chat_id] = "WAITING_DOCX"
        await update.message.reply_text("📎 Hãy bấm biểu tượng Kẹp ghim và gửi tệp Word (.docx) vào đây:")
        return

    if text == "🎧 Tìm nội dung âm thanh mới":
        await update.message.reply_text("🔎 Đang quét sản phẩm FS Equipment và tin ngành âm thanh...")
        await scan_and_send_hot_news(context.bot, str(chat_id), is_manual=True)
        return

    if current_state == "WAITING_EDIT_VOICE" and session:
        session["meta"]["voice_script"] = text
        USER_STATES[chat_id] = None
        control_msg_id = session.get("control_msg_id")
        if control_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=control_msg_id,
                    text=format_preview_text(session["meta"]), parse_mode="HTML",
                    reply_markup=build_media_management_keyboard(session["media_status"])
                )
            except Exception:
                pass
        await update.message.reply_text("✅ Đã cập nhật lời đọc thành công!")
        return

    if current_state == "WAITING_EDIT_TITLE" and session:
        session["meta"]["title"] = text.upper()
        USER_STATES[chat_id] = None
        control_msg_id = session.get("control_msg_id")
        if control_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=control_msg_id,
                    text=format_preview_text(session["meta"]), parse_mode="HTML",
                    reply_markup=build_media_management_keyboard(session["media_status"])
                )
            except Exception:
                pass
        await update.message.reply_text("✅ Đã cập nhật tiêu đề banner thành công!")
        return

    if current_state == "WAITING_RAW_CONTENT" or (len(text) > 80 and not text.startswith("http")):
        USER_STATES[chat_id] = None
        await init_session_with_content(text, [], "Nhập tay tự do", chat_id, context)
        return

    if text.startswith(("http://", "https://")) or current_state == "WAITING_URL":
        USER_STATES[chat_id] = None
        status_msg = await update.message.reply_text("🔍 Đang đọc bài báo và trích xuất tư liệu...")
        news_text, images = fetch_article_and_images(text, max_images=6)
        await status_msg.delete()
        if not news_text:
            await update.message.reply_text("❌ Không thể trích xuất nội dung từ bài viết này.")
            return
        await init_session_with_content(news_text, images, text, chat_id, context)
        return

    await update.message.reply_text("💡 Vui lòng bấm một trong các nút chức năng bên dưới bàn phím để thao tác.")

async def handle_incoming_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if ALLOWED_CHAT_IDS and str(chat_id) not in ALLOWED_CHAT_IDS:
        return

    session = USER_SESSIONS.get(chat_id)
    caption_text = update.message.caption.strip() if update.message.caption else ""
    photo = update.message.photo[-1]

    if not session and len(caption_text) > 40:
        new_img_path = str(TEMP_DIR / f"custom_img_1_{photo.file_unique_id}.jpg")
        await (await photo.get_file()).download_to_drive(new_img_path)
        await init_session_with_content(caption_text, [new_img_path], "Ảnh kèm bài", chat_id, context)
        return

    if not session:
        await update.message.reply_text("⚠️ Hãy gửi nội dung bài viết trước khi gửi ảnh tư liệu.")
        return

    new_idx = len(session["media_status"]) + 1
    custom_img_path = str(TEMP_DIR / f"custom_img_{new_idx}_{photo.file_unique_id}.jpg")
    await (await photo.get_file()).download_to_drive(custom_img_path)
    session["media_status"].append([custom_img_path, True, 'image'])

    control_msg_id = session.get("control_msg_id")
    if control_msg_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=control_msg_id,
                reply_markup=build_media_management_keyboard(session["media_status"])
            )
        except Exception:
            pass
    await update.message.reply_text(f"📸 Đã thêm Ảnh #{new_idx}. Bạn có thể đổi thứ tự trên bàn phím.")

async def handle_incoming_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if ALLOWED_CHAT_IDS and str(chat_id) not in ALLOWED_CHAT_IDS:
        return

    session = USER_SESSIONS.get(chat_id)
    if not session:
        await update.message.reply_text("⚠️ Hãy gửi nội dung bài viết trước khi gửi video tư liệu.")
        return

    message = update.effective_message
    video_item = message.video or message.document
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", video_item.file_name or "video.mp4")
    local_path = str(TEMP_DIR / f"custom_vid_{int(time.time())}_{safe_name}")

    msg_wait = await update.message.reply_text("📥 Đang tải video tư liệu xuống máy chủ...")
    await (await video_item.get_file()).download_to_drive(local_path)
    await msg_wait.delete()

    session["media_status"].append([local_path, True, "video"])
    control_msg_id = session.get("control_msg_id")
    if control_msg_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=control_msg_id,
                reply_markup=build_media_management_keyboard(session["media_status"])
            )
        except Exception:
            pass
    await update.message.reply_text(f"🎬 Đã thêm Video tư liệu #{len(session['media_status'])}.")

async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file_name = (doc.file_name or "").lower()
    if Path(file_name).suffix in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
        await handle_incoming_video(update, context)
        return

    if not file_name.endswith((".docx", ".doc")):
        await update.message.reply_text("⚠️ Vui lòng gửi file Word (.docx)!")
        return

    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text("🔍 Đang tải và bóc tách tệp Word...")
    local_docx_path = str(TEMP_DIR / doc.file_name)
    await (await doc.get_file()).download_to_drive(local_docx_path)
    news_text, images = extract_docx_content_and_images(local_docx_path, max_images=6)
    await status_msg.delete()

    if not news_text:
        await update.message.reply_text("❌ Không thể đọc nội dung văn bản từ tệp Word.")
        return
    await init_session_with_content(news_text, images, doc.file_name, chat_id, context)

# -------------------------------------------------------------
# XỬ LÝ NÚT BẤM CALLBACK QUERY
# -------------------------------------------------------------
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data.startswith("news_pick_"):
        article_id = int(query.data.split("_")[-1])
        article_url = get_link_by_id(article_id)
        if article_url:
            status_msg = await query.message.reply_text("🔍 Đang lấy nội dung và tư liệu từ nguồn đã chọn...")
            news_text, images = fetch_article_and_images(article_url, max_images=6)
            await status_msg.delete()
            if news_text:
                await init_session_with_content(news_text, images, article_url, chat_id, context)
        return

    session = USER_SESSIONS.get(chat_id)
    if not session and query.data != "cancel_action":
        await query.edit_message_text("⚠️ Phiên làm việc đã hết hạn.")
        return

    if query.data == "cancel_action":
        USER_SESSIONS.pop(chat_id, None)
        USER_STATES[chat_id] = None
        await query.edit_message_text("❌ Đã hủy phiên làm việc.")
        return

    if query.data == "edit_voice_script":
        USER_STATES[chat_id] = "WAITING_EDIT_VOICE"
        await query.message.reply_text("🎙️ Hãy gửi lại toàn bộ lời đọc phát thanh mới vào đây:")
        return

    if query.data == "edit_title":
        USER_STATES[chat_id] = "WAITING_EDIT_TITLE"
        await query.message.reply_text("🏷️ Hãy gửi tiêu đề banner mới (dưới 12 từ):")
        return

    if query.data.startswith("move_up_") or query.data.startswith("move_down_"):
        direction, idx_str = query.data.rsplit("_", 1)
        idx = int(idx_str)
        target = idx - 1 if direction == "move_up" else idx + 1
        items = session["media_status"]
        if 0 <= idx < len(items) and 0 <= target < len(items):
            items[idx], items[target] = items[target], items[idx]
        await query.edit_message_reply_markup(reply_markup=build_media_management_keyboard(items))
        return

    if query.data.startswith("toggle_"):
        idx = int(query.data.split("_")[1])
        session["media_status"][idx][1] = not session["media_status"][idx][1]
        await query.edit_message_reply_markup(reply_markup=build_media_management_keyboard(session["media_status"]))
        return

    if query.data in ["render_and_publish", "render_only"]:
        auto_publish = (query.data == "render_and_publish")
        selected_media = [
            {"path": path, "enabled": selected, "type": m_type}
            for path, selected, m_type in session["media_status"] if selected and os.path.exists(path)
        ]
        meta = session["meta"]
        output_video = BASE_DIR / "final_fs_equipment_video.mp4"

        await query.edit_message_text(
            f"⏳ <b>Đang dựng video với {len(selected_media)} tư liệu đã chọn...</b>\n"
            "1. Đang tổng hợp giọng đọc phát thanh...\n"
            "2. Đang phân bổ Video-first và hiệu ứng Ken Burns...\n"
            "3. Đang render video Full HD 1080p...",
            parse_mode="HTML"
        )

        try:
            audio_file = str(TEMP_DIR / "voice.mp3")
            await text_to_speech(meta["voice_script"], audio_file)

            audio_duration = probe_media_duration(audio_file)
            timeline = allocate_media_timeline(selected_media, audio_duration)
            video_segments = []
            img_counter = 0

            for idx, item in enumerate(timeline, start=1):
                clip_path = str(TEMP_DIR / f"timeline_clip_{idx}.mp4")
                if item["type"] == "video":
                    prepare_video_asset(item["path"], clip_path, item["duration"])
                else:
                    convert_image_to_video_clip(item["path"], clip_path, duration=item["duration"], effect_idx=img_counter)
                    img_counter += 1
                video_segments.append(clip_path)

            render_final_video(
                audio_path=audio_file,
                video_clips=video_segments,
                output_file=str(output_video),
                title_text=meta["title"],
                voice_script=meta.get("voice_script", "")
            )

            await context.bot.send_message(chat_id=chat_id, text="🎬 <b>ĐÃ RENDER XONG!</b> Đang gửi video...", parse_mode="HTML")
            with open(output_video, "rb") as video_file:
                await context.bot.send_video(
                    chat_id=chat_id, video=video_file, caption=f"🎥 {meta['title']}",
                    supports_streaming=True, read_timeout=300, write_timeout=300
                )

            copy_ready = html.escape(f"{meta.get('caption', '')}\n\n{meta.get('hashtags', '')}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📱 <b>NỘI DUNG ĐĂNG BÀI:</b>\n\n🏷️ <b>Tiêu đề:</b>\n<code>{html.escape(meta['title'])}</code>\n\n📋 <b>Caption:</b>\n<pre><code>{copy_ready}</code></pre>",
                parse_mode="HTML"
            )

            yt_published_url = ""
            fb_published_id = ""

            if auto_publish:
                status_publish = await context.bot.send_message(chat_id=chat_id, text="🌐 Đang đăng lên YouTube Shorts & Facebook Reels...")
                logs = []
                caption_full = f"{meta['title']}\n\n{meta['caption']}\n\n{meta['hashtags']}"

                if upload_youtube_shorts:
                    try:
                        yt_url = upload_youtube_shorts(str(output_video), meta["title"], caption_full, "public")
                        if yt_url:
                            yt_published_url = yt_url
                            logs.append(f"🔴 YouTube Shorts: {yt_url}")
                    except Exception as e:
                        logs.append(f"❌ YouTube lỗi: {e}")

                if upload_facebook_reels and FB_PAGE_ID and FB_ACCESS_TOKEN:
                    try:
                        fb_id = upload_facebook_reels(str(output_video), caption_full, FB_PAGE_ID, FB_ACCESS_TOKEN)
                        if fb_id:
                            fb_published_id = fb_id
                            logs.append(f"🔵 Facebook Reels: Thành công (ID: {fb_id})")
                    except Exception as e:
                        logs.append(f"❌ Facebook lỗi: {e}")

                await status_publish.edit_text("🚀 <b>KẾT QUẢ ĐĂNG BÀI:</b>\n\n" + "\n".join(logs), parse_mode="HTML")

            save_to_dashboard_json(meta, yt_url=yt_published_url, fb_id=fb_published_id)
            USER_SESSIONS.pop(chat_id, None)
            USER_STATES[chat_id] = None

        except Exception as e:
            logging.error(f"Lỗi dựng video: {e}")
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Xảy ra lỗi khi dựng video:\n{e}")

# -------------------------------------------------------------
# KHỞI CHẠY BOT
# -------------------------------------------------------------
async def post_init(application):
    daily_cleanup_scheduler.app = application
    asyncio.create_task(periodic_news_scanner(application))
    asyncio.create_task(daily_cleanup_scheduler())

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN trong file .env.")
    if not ALLOWED_CHAT_IDS:
        raise RuntimeError("Thiếu ALLOWED_CHAT_ID trong file .env; cần ít nhất một chat ID được phép.")
    init_news_db()
    request_client = HTTPXRequest(connect_timeout=90.0, read_timeout=600.0, write_timeout=600.0, pool_timeout=90.0)
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request_client)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("clean", clean_command))
    application.add_handler(CommandHandler("scan", lambda u, c: scan_and_send_hot_news(c.bot, str(u.effective_chat.id), is_manual=True)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_incoming_photo))
    application.add_handler(MessageHandler(filters.VIDEO, handle_incoming_video))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document_message))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    print("🤖 Telegram Bot FS Equipment đã sẵn sàng...")
    application.run_polling()

if __name__ == "__main__":
    main()