import os
import re
import time
import html
import sqlite3
from datetime import datetime

import feedparser
import requests
from deep_translator import GoogleTranslator


# =========================
# 基础配置
# =========================

RSS_URLS = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # 你的频道，例如 @btc8688
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # 默认 5 分钟
SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))  # 每条消息之间间隔
MAX_SUMMARY_LENGTH = int(os.getenv("MAX_SUMMARY_LENGTH", "500"))  # 摘要最长截取
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


def safe_translate(text: str) -> str:
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target="zh-CN").translate(text)
    except Exception as e:
        print(f"翻译失败: {e}")
        return text


def shorten_text(text: str, max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def get_source_name(entry, feed) -> str:
    # 优先取 feed 标题
    source = getattr(feed.feed, "title", "").strip()
    if source:
        return source

    # 其次根据链接域名简单判断
    link = getattr(entry, "link", "")
    if "bloomberg.com" in link:
        return "Bloomberg"
    if "cnbc.com" in link:
        return "CNBC"
    if "coindesk.com" in link:
        return "CoinDesk"
    if "cointelegraph.com" in link:
        return "Cointelegraph"
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
        "#汽车": ["geely", "tesla", "auto", "car", "汽车", "吉利", "特斯拉"],
    }

    for tag, keywords in keyword_map.items():
        if any(k in text for k in keywords):
            tags.append(tag)

    # 限制标签数量，避免太乱
    return tags[:3]


def format_message(title_cn: str, summary_cn: str, title_en: str, link: str, source: str, tags: list) -> str:
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
    parts.append(f"原文：{title_en.strip()}")
    parts.append(f"来源：{source}")
    parts.append("")
    parts.append(link.strip())

    msg = "\n".join(parts).strip()

    # Telegram 单条消息长度限制，保守处理
    if len(msg) > 3500:
        msg = msg[:3500].rstrip() + "..."
    return msg


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": False
        },
        timeout=30
    )
    print("发送结果:", resp.status_code, resp.text)


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
    entries.reverse()  # 从旧到新发

    first_run = not has_any_sent_items()

    for entry in entries:
        link = getattr(entry, "link", "").strip()
        title_en = clean_html(getattr(entry, "title", "").strip())

        # 优先取 summary，没有就取 description
        raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
        summary_clean = clean_html(raw_summary)
        summary_clean = shorten_text(summary_clean, MAX_SUMMARY_LENGTH)

        if not link or not title_en:
            continue

        if has_sent(link):
            continue

        # 第一次启动：只记录旧新闻，不推送，防止刷屏
        if first_run and FIRST_RUN_SKIP_OLD:
            print("首次运行，跳过旧新闻:", title_en)
            mark_sent(link)
            continue

        title_cn = safe_translate(title_en)
        summary_cn = safe_translate(summary_clean) if summary_clean else ""

        source = get_source_name(entry, feed)
        tags = detect_tags(title_en, title_cn, summary_cn)

        msg = format_message(
            title_cn=title_cn,
            summary_cn=summary_cn,
            title_en=title_en,
            link=link,
            source=source,
            tags=tags
        )

        try:
            send_telegram_message(msg)
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

    print("翻译机器人启动成功")
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
