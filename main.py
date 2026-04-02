import os
import re
import time
import html
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

import feedparser
import requests
from deep_translator import GoogleTranslator


# =========================
# 基础配置
# =========================

RSS_URLS = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # 例如 @你的频道用户名
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))
MAX_SUMMARY_LENGTH = int(os.getenv("MAX_SUMMARY_LENGTH", "1200"))
FIRST_RUN_SKIP_OLD = os.getenv("FIRST_RUN_SKIP_OLD", "true").lower() == "true"


# =========================
# 数据库
# =========================

def init_db():
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_items (
            link TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def has_sent(link: str) -> bool:
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_items WHERE link = ?", (link,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_sent(link: str):
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO sent_items(link, created_at) VALUES (?, ?)",
        (link, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def has_any_sent_items() -> bool:
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sent_items")
    count = cur.fetchone()[0]
    conn.close()
    return count > 0


# =========================
# 工具函数
# =========================

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<.*?>", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def shorten_text(text: str, max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def safe_translate(text: str) -> str:
    """
    规则：
    - 翻译成功返回中文
    - 翻译失败返回空字符串
    - 不返回英文原文
    """
    if not text:
        return ""

    text = text.strip()
    if not text:
        return ""

    # 稍微放宽一点，尽量保留更多摘要
    if len(text) > 1500:
        text = text[:1500]

    for i in range(3):
        try:
            result = GoogleTranslator(source="auto", target="zh-CN").translate(text)
            if result and result.strip():
                return result.strip()
        except Exception as e:
            print(f"翻译失败，第{i + 1}次: {e}")
            time.sleep(1)

    return ""


def get_source_name(entry, feed) -> str:
    source = getattr(feed.feed, "title", "").strip()
    if source:
        return source

    link = getattr(entry, "link", "")
    if "bloomberg.com" in link:
        return "Bloomberg"
    if "coindesk.com" in link:
        return "CoinDesk"
    if "cointelegraph.com" in link:
        return "Cointelegraph"
    if "decrypt.co" in link:
        return "Decrypt"
    if "dj.com" in link:
        return "US Top News and Analysis"
    return "未知来源"


def detect_tags(title_en: str, title_cn: str, summary_cn: str) -> list:
    text = f"{title_en}\n{title_cn}\n{summary_cn}".lower()
    tags = []

    keyword_map = {
        "#BTC": ["bitcoin", "btc", "比特币"],
        "#ETH": ["ethereum", "eth", "以太坊"],
        "#ETF": ["etf"],
        "#监管": ["sec", "regulation", "regulator", "监管", "执法"],
        "#美股": ["stocks", "wall street", "nasdaq", "dow", "s&p 500", "美股", "股市"],
        "#宏观": ["inflation", "cpi", "ppi", "federal reserve", "fed", "interest rate", "通胀", "利率", "美联储"],
        "#原油": ["oil", "crude", "原油"],
        "#黄金": ["gold", "黄金"],
        "#交易所": ["binance", "coinbase", "kraken", "exchange", "交易所"],
        "#链上": ["blockchain", "on-chain", "链上"],
        "#AI": ["ai", "artificial intelligence", "人工智能"],
    }

    for tag, keywords in keyword_map.items():
        if any(k in text for k in keywords):
            tags.append(tag)

    return tags[:3]


def get_image_url(entry) -> str:
    # 1. media_content
    media_content = getattr(entry, "media_content", None)
    if media_content and isinstance(media_content, list):
        for item in media_content:
            url = item.get("url")
            if url:
                return url

    # 2. media_thumbnail
    media_thumbnail = getattr(entry, "media_thumbnail", None)
    if media_thumbnail and isinstance(media_thumbnail, list):
        for item in media_thumbnail:
            url = item.get("url")
            if url:
                return url

    # 3. enclosure / image
    links = getattr(entry, "links", [])
    if links:
        for item in links:
            href = item.get("href", "")
            type_ = item.get("type", "")
            rel = item.get("rel", "")
            if href and (rel == "enclosure" or str(type_).startswith("image/")):
                return href

    # 4. 从 summary / description 中提取 img
    raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
    if raw_summary:
        m = re.search(r'<img[^>]+src="([^"]+)"', raw_summary, re.I)
        if m:
            return m.group(1)

    return ""


def is_valid_http_url(url: str) -> bool:
    if not url:
        return False
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def extract_best_summary(entry) -> str:
    """
    尽量提取更长、更像正文的摘要
    优先顺序：
    content > summary_detail > summary > description
    """
    candidates = []

    content = getattr(entry, "content", None)
    if content and isinstance(content, list):
        for item in content:
            value = item.get("value", "")
            if value:
                candidates.append(value)

    summary_detail = getattr(entry, "summary_detail", None)
    if summary_detail and isinstance(summary_detail, dict):
        value = summary_detail.get("value", "")
        if value:
           
