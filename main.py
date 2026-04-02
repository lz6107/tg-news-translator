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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))   # 默认 5 分钟
SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))           # 每条消息之间间隔
MAX_SUMMARY_LENGTH = int(os.getenv("MAX_SUMMARY_LENGTH", "900"))
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

    if len(text) > 1200:
        text = text[:1200]

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
    media_content = getattr(entry, "media_content", None)
    if media_content and isinstance(media_content, list):
        for item in media_content:
            url = item.get("url")
            if url:
                return url

    media_thumbnail = getattr(entry, "media_thumbnail", None)
    if media_thumbnail and isinstance(media_thumbnail, list):
        for item in media_thumbnail:
            url = item.get("url")
            if url:
                return url

    links = getattr(entry, "links", [])
    if links:
        for item in links:
            href = item.get("href", "")
            type_ = item.get("type", "")
            rel = item.get("rel", "")
            if href and (rel == "enclosure" or str(type_).startswith("image/")):
                return href

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


def build_caption(title_cn: str, summary_cn: str, source: str, tags: list) -> str:
    """
    只发中文，不带链接，不带英文原文
    """
    header = "【财经翻译】"
    tag_line = " ".join(tags).strip()

    parts = [header]
    if tag_line:
        parts.append(tag_line)

    parts.append("")
    parts.append(title_cn.strip())

    if summary_cn.strip():
        parts.append("")
        parts.append(summary_cn.strip())

    parts.append("")
    parts.append(f"来源：{source}")

    caption = "\n".join(parts).strip()

    # Telegram photo caption 上限 1024
    if len(caption) > 1000:
        caption = caption[:1000].rstrip() + "..."
    return caption


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True
        },
        timeout=30
    )
    print("sendMessage 结果:", resp.status_code, resp.text)
    return resp


def send_telegram_photo(photo_url: str, caption: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "photo": photo_url,
            "caption": caption
        },
        timeout=30
    )
    print("sendPhoto 结果:", resp.status_code, resp.text)
    return resp


def extract_summary(entry) -> str:
    """
    尽量提取更长的正文摘要
    """
    raw_summary = (
        getattr(entry, "summary", "")
        or getattr(entry, "description", "")
    )

    # 一些源会把正文片段放在 content 里
    content_list = getattr(entry, "content", None)
    if content_list and isinstance(content_list, list):
        for item in content_list:
            value = item.get("value", "")
            if value and len(value) > len(raw_summary):
                raw_summary = value

    summary_clean = clean_html(raw_summary)
    summary_clean = re.sub(r"\s+", " ", summary_clean).strip()

    # 太短就视为没有摘要
    if len(summary_clean) < 40:
        return ""

    return shorten_text(summary_clean, MAX_SUMMARY_LENGTH)


# =========================
# 核心处理
# =========================

def process_feed(feed_url: str):
    print(f"[{datetime.now()}] 检查 RSS: {feed_url}")
    feed = feedparser.parse(feed_url)

    if not feed.entries:
        print("没有抓到内容")
        return

    entries = list(feed.entries[:10])
    entries.reverse()

    first_run = not has_any_sent_items()

    for entry in entries:
        link = getattr(entry, "link", "").strip()
        title_en = clean_html(getattr(entry, "title", "").strip())

        if not link or not title_en:
            continue

        if has_sent(link):
            continue

        if first_run and FIRST_RUN_SKIP_OLD:
            print("首次运行，跳过旧新闻:", title_en)
            mark_sent(link)
            continue

        summary_clean = extract_summary(entry)

        title_cn = safe_translate(title_en)
        summary_cn = safe_translate(summary_clean) if summary_clean else ""

        # 标题翻译失败：整条跳过
        if not title_cn:
            print("跳过：标题翻译失败 ->", title_en)
            mark_sent(link)
            continue

        # 摘要翻译失败：允许只发标题
        if summary_clean and not summary_cn:
            print("摘要翻译失败，仅发送标题 ->", title_en)
            summary_cn = ""

        # 摘要翻出来太短，也直接清空
        if summary_cn and len(summary_cn.strip()) < 15:
            summary_cn = ""

        source = get_source_name(entry, feed)
        tags = detect_tags(title_en, title_cn, summary_cn)
        caption = build_caption(title_cn, summary_cn, source, tags)

        image_url = get_image_url(entry)

        try:
            if is_valid_http_url(image_url):
                resp = send_telegram_photo(image_url, caption)
                if resp.status_code != 200:
                    print("图片发送失败，改为纯文字")
                    send_telegram_message(caption)
            else:
                send_telegram_message(caption)

            mark_sent(link)
            print("已发送:", title_en)

        except Exception as e:
            print("发送失败:", e)

        time.sleep(SEND_DELAY)


def main():
    if not BOT_TOKEN:
        raise ValueError("缺少环境变量 BOT_TOKEN")
    if not CHAT_ID:
        raise ValueError("缺少环境变量 CHAT_ID")

    init_db()

    print("翻译机器人启动成功（长摘要图文无链接版）")
    print("频道:", CHAT_ID)

    while True:
        for rss in RSS_URLS:
            try:
                process_feed(rss)
            except Exception as e:
                print(f"处理 RSS 失败 {rss}: {e}")

        print(f"休眠 {CHECK_INTERVAL} 秒...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
