import os
import time
import sqlite3
import asyncio
import logging
import html
import feedparser
import schedule
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TimedOut, NetworkError, RetryAfter
from telegram.request import HTTPXRequest

# ------------------------------------------------------------------
# CẤU HÌNH BOT TELEGRAM & DANH SÁCH NGUỒN TIN NÓNG (RSS)
# ------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Danh sách nguồn RSS tin nóng / thời sự cập nhật liên tục
RSS_FEEDS = [
    # Tin tổng hợp và thời sự
    {"source": "VnExpress Tin mới", "url": "https://vnexpress.net/rss/tin-moi-nhat.rss"},
    {"source": "VnExpress Thời sự", "url": "https://vnexpress.net/rss/thoi-su.rss"},
    {"source": "VnExpress Kinh doanh", "url": "https://vnexpress.net/rss/kinh-doanh.rss"},
    {"source": "VnExpress Pháp luật", "url": "https://vnexpress.net/rss/phap-luat.rss"},
    {"source": "VnExpress Bất động sản", "url": "https://vnexpress.net/rss/bat-dong-san.rss"},
    {"source": "VnExpress Khoa học công nghệ", "url": "https://vnexpress.net/rss/khoa-hoc-cong-nghe.rss"},

    # Thời sự, chính sách, kinh tế và xã hội
    {"source": "Tuổi Trẻ Thời sự", "url": "https://tuoitre.vn/rss/thoi-su.rss"},
    {"source": "Tuổi Trẻ Pháp luật", "url": "https://tuoitre.vn/rss/phap-luat.rss"},
    {"source": "Tuổi Trẻ Kinh doanh", "url": "https://tuoitre.vn/rss/kinh-doanh.rss"},
    {"source": "Tuổi Trẻ Khoa học", "url": "https://tuoitre.vn/rss/khoa-hoc.rss"},
    {"source": "Thanh Niên Thời sự", "url": "https://thanhnien.vn/rss/thoi-su.rss"},
    {"source": "Thanh Niên Chính trị", "url": "https://thanhnien.vn/rss/chinh-tri.rss"},
    {"source": "Thanh Niên Kinh tế", "url": "https://thanhnien.vn/rss/kinh-te.rss"},
    {"source": "Thanh Niên Doanh nghiệp", "url": "https://thanhnien.vn/rss/kinh-te/doanh-nghiep.rss"},
    {"source": "Thanh Niên Pháp luật", "url": "https://thanhnien.vn/rss/thoi-su/phap-luat.rss"},
    {"source": "Thanh Niên Dân sinh", "url": "https://thanhnien.vn/rss/thoi-su/dan-sinh.rss"},
    {"source": "Thanh Niên Lao động - Việc làm", "url": "https://thanhnien.vn/rss/thoi-su/lao-dong-viec-lam.rss"},
    {"source": "Dân Trí Xã hội", "url": "https://dantri.com.vn/rss/xa-hoi.rss"},
    {"source": "Dân Trí Kinh doanh", "url": "https://dantri.com.vn/rss/kinh-doanh.rss"},
    {"source": "Dân Trí Pháp luật", "url": "https://dantri.com.vn/rss/phap-luat.rss"},
]

HOT_KEYWORDS = [
    # Chính trị - điều hành - chính sách
    "thủ tướng chỉ đạo", "chính phủ yêu cầu", "quốc hội thông qua",
    "ban hành nghị định", "ban hành thông tư", "nghị quyết mới",
    "chính sách mới", "quyết định mới", "có hiệu lực từ hôm nay",
    "đề xuất tăng", "đề xuất giảm", "điều chỉnh quy định",
    "cải cách thủ tục hành chính", "cắt giảm điều kiện kinh doanh",

    # Pháp luật - thanh tra - xử lý vi phạm
    "khởi tố", "bắt tạm giam", "bị bắt", "truy tố", "xét xử",
    "tuyên án", "điều tra", "triệt phá", "thanh tra đột xuất",
    "kết luận thanh tra", "xử phạt", "phạt tiền", "đình chỉ hoạt động",
    "thu hồi giấy phép", "thu hồi sản phẩm", "khởi kiện",
    "vi phạm pháp luật", "hàng giả", "hàng nhái", "lừa đảo",
    "tham nhũng", "thất thoát", "sai phạm", "cưỡng chế",

    # Doanh nghiệp - thị trường - tài chính
    "phá sản", "giải thể", "ngừng hoạt động", "sa thải hàng loạt",
    "nợ lương", "chậm trả lương", "nợ bảo hiểm xã hội",
    "bị xử phạt", "đóng cửa", "thu hồi trái phiếu",
    "cổ phiếu giảm sàn", "giá xăng tăng", "giá xăng giảm",
    "giá điện tăng", "giá vàng lập đỉnh", "lãi suất điều chỉnh",
    "cảnh báo lừa đảo đầu tư", "mất khả năng thanh toán",

    # Đất đai - bất động sản - xây dựng
    "sốt đất", "sốt giá bất động sản", "dự án bị đình chỉ",
    "dự án bị thu hồi", "chậm tiến độ", "vi phạm xây dựng",
    "cưỡng chế công trình", "quy hoạch mới", "bảng giá đất",
    "thu hồi đất", "giải phóng mặt bằng", "sổ đỏ", "sổ hồng",

    # An sinh - lao động - người có công - người khuyết tật
    "tăng lương cơ sở", "tăng lương tối thiểu", "điều chỉnh lương hưu",
    "mức hưởng bảo hiểm xã hội", "trợ cấp xã hội", "trợ cấp người có công",
    "hỗ trợ người khuyết tật", "tai nạn lao động", "đình công",
    "sa thải người lao động", "nợ bảo hiểm", "chính sách người có công",

    # Tai nạn - thiên tai - y tế - cảnh báo
    "khẩn cấp", "cảnh báo khẩn", "cảnh báo thiên tai",
    "mưa lũ", "ngập lụt", "sạt lở", "bão đổ bộ",
    "động đất", "cháy lớn", "tai nạn nghiêm trọng",
    "nhiều người thương vong", "ngộ độc", "dịch bệnh",
    "thu hồi thuốc", "thu hồi thực phẩm", "cảnh báo sức khỏe",

    # Khoa học - công nghệ - an ninh mạng
    "rò rỉ dữ liệu", "tấn công mạng", "lộ dữ liệu cá nhân",
    "cảnh báo an ninh mạng", "lỗ hổng bảo mật", "giả mạo bằng AI",
    "trí tuệ nhân tạo", "công nghệ mới", "đổi mới sáng tạo"
]