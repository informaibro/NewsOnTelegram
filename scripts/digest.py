#!/usr/bin/env python3
import os
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from dateutil import parser as dparser
from bs4 import BeautifulSoup
import html2text
import time

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID environment variables (set as GitHub secrets).")

# Curated RSS/news sources (edit to match your preferred outlets)
RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://www.reuters.com/technology/rss",
    "https://platformer.news/feed.xml",
    "https://aifundingtracker.com/feed",
    "https://aijourn.com/feed"
]

def fetch_feed_entries(url):
    try:
        d = feedparser.parse(url)
        return d.entries if 'entries' in d else []
    except Exception as e:
        print("feed error", url, e)
        return []

def text_from_url(url):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for s in soup(["script","style","noscript"]):
            s.decompose()
        text = soup.get_text(separator="\n")
        return text.strip()
    except Exception as e:
        print("fetch error", url, e)
        return ""

def summarize_with_openai(title, content, url):
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        prompt = (
            f"Write a concise, non-technical 50-70 word summary (3-4 short sentences) for the following news item. "
            f"Include: what happened, who (company/people), context/implication, and one short 'Founder action' line. "
            f"Keep it plain English for founders/entrepreneurs. Include the original title and url at the top.\n\n"
            f"Title: {title}\nURL: {url}\n\nArticle text:\n{content[:8000]}\n\nSummary:"
        )
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # change if you have other preferred model
            messages=[{"role":"user","content":prompt}],
            max_tokens=220,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("OpenAI error:", e)
        return None

def simple_extract_summary(title, content, url):
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    top = " ".join(lines[:3]) if lines else title
    action = "Founder action: Monitor vendor integrations and prioritize pilots with measurable KPIs."
    return f"{title}\n{top}\n{action}\n{url}"

def post_telegram(text):
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
"parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    r = requests.post(api, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()

def main():
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)
    items = []

    for feed in RSS_FEEDS:
        entries = fetch_feed_entries(feed)
        for e in entries:
            published = None
            if hasattr(e, 'published'):
                try:
                    published = dparser.parse(e.published)
                except:
                    published = None
            if not published and hasattr(e, 'updated'):
                try:
                    published = dparser.parse(e.updated)
                except:
                    published = None
            if published and published < since:
                continue
            title = getattr(e, 'title', '') or ''
            link = getattr(e, 'link', '') or ''
            summary = getattr(e, 'summary', '') or ''
            items.append({"title": title, "link": link, "summary": summary, "published": published})

    items = sorted(items, key=lambda x: x.get("published") or now, reverse=True)

    seen = set()
    dedup = []
    for it in items:
        key = (it["link"] or it["title"]).strip()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(it)
    items = dedup[:30]

    enriched = []
    for it in items:
        link = it["link"]
        content = it.get("summary") or ""
        if link and not content:
            content = text_from_url(link)
            time.sleep(0.3)
        summary = None
        if OPENAI_API_KEY:
            summary = summarize_with_openai(it["title"], content, link)
        if not summary:
            summary = simple_extract_summary(it["title"], content, link)
        enriched.append({"title": it["title"], "link": link, "summary": summary})

    top5 = enriched[:5]
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    header = f"<b>AI Brief — {date_str}</b>\n\n"

    body = ""
    for i, e in enumerate(top5, start=1):
        body += f"<b>{i}. {e['title']}</b>\n{html2text.html2text(e['summary'])}\n\n"

    want_more = enriched[5:13]
    if want_more:
        body += "<b>Want more (quick links)</b>\n"
        for w in want_more:
            if w['link']:
                body += f"• {w['title']} — {w['link']}\n"
            else:
                body += f"• {w['title']}\n"

    message = header + body
    if len(message) > 3800:
        message = message[:3800] + "\n\n... (truncated)"

    resp = post_telegram(message)
    print("Posted:", resp)

if __name__ == "__main__":
    main() 
