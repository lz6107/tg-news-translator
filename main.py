import feedparser
import requests
from deep_translator import GoogleTranslator
import time
import os

RSS = "https://feeds.bloomberg.com/markets/news.rss"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

sent_links = set()

def send(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url,data={
        "chat_id":CHAT_ID,
        "text":msg,
        "disable_web_page_preview":False
    })

while True:

    feed = feedparser.parse(RSS)

    for entry in feed.entries[:5]:

        title = entry.title
        link = entry.link

        if link in sent_links:
            continue

        sent_links.add(link)

        cn = GoogleTranslator(source='auto',target='zh-CN').translate(title)

        msg = f"""【财经翻译】

{cn}

原文:
{title}

{link}
"""

        send(msg)

        time.sleep(3)

    time.sleep(300)
