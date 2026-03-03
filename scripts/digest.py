#!/usr/bin/env python3
import os
import sys
import requests
import feedparser
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta, timezone
from dateutil import parser as dparser
from bs4 import BeautifulSoup
import html2text
import time
import re
import traceback

# Config from environment / GitHub secrets
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MAIL_EMAIL = os.getenv("MAIL_EMAIL")
MAIL_APP_PASSWORD = os.getenv("MAIL_APP_PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID environment variables (set as GitHub secrets).")

# RSS feeds (starter list — edit as you like)
RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://www.reuters.com/technology/rss",
    "https://aifundingtracker.com/feed",
    "https://aijourn.com/feed"
]

# Helper: fetch feed entries
def fetch_feed_entries(url):
    try:
        d = feedparser.parse(url)
        return d.entries if 'entries' in d else []
    except Exception as e:
        print(f"[RSS] feed error {url}: {e}")
        return []

# Helper: fetch HTML page text
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
        print(f"[FETCH] fetch error {url}: {e}")
        return ""

# OpenAI summarization (optional)
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
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            max_tokens=220,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("[OpenAI] error:", e)
        return None
        # Fallback summary extractor
def simple_extract_summary(title, content, url):
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    top = " ".join(lines[:4]) if lines else title
    action = "Founder action: Monitor vendor integrations and prioritize pilots with measurable KPIs."
    return f"{title}\n{top}\n{action}\n{url}"

# Telegram post helper
def post_telegram(text):
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    r = requests.post(api, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

# -- IMAP / mailbox reading --
def decode_mime_words(s):
    try:
        parts = decode_header(s)
        out = []
        for part, enc in parts:
            if isinstance(part, bytes):
                out.append(part.decode(enc or 'utf-8', errors='ignore'))
            else:
                out.append(part)
        return ''.join(out)
    except Exception:
        return s

def extract_links_from_html(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    links = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        text = a.get_text(separator=' ', strip=True)
        if href and text:
            links.append((text, href))
    return links

def parse_message_body(msg):
    # prefer HTML, fallback to text/plain
    body = ""
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/html" and "attachment" not in disp:
                html = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                try:
                    html = html.decode(charset, errors='ignore')
                except:
                    html = html.decode('utf-8', errors='ignore')
                break
        if not html:
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    text = part.get_payload(decode=True)
                    try:
                        text = text.decode(part.get_content_charset() or 'utf-8', errors='ignore')
                    except:
                        text = text.decode('utf-8', errors='ignore')
                    body = text
                    break
    else:
        ctype = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                body = payload.decode(msg.get_content_charset() or 'utf-8', errors='ignore')
            except:
                body = payload.decode('utf-8', errors='ignore')
    if html:
        # extract main text from HTML and preserve links
        links = extract_links_from_html(html)
        text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
        return text, links
    return body, []

def imap_fetch_newsletters(email_addr, app_password, days_back=7, max_messages=50):
    results = []
    if not email_addr or not app_password:
        print("[MAIL] No MAIL_EMAIL or MAIL_APP_PASSWORD provided; skipping mailbox read.")
        return results

    try:
        print(f"[MAIL] Connecting to IMAP {IMAP_SERVER}:{IMAP_PORT} as {email_addr}")
        M = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        M.login(email_addr, app_password)
        M.select("INBOX")
        since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        # Search recent messages. We search for UNSEEN OR SINCE since_date to capture recent newsletters.
        typ, data = M.search(None, f'(OR UNSEEN SINCE {since_date})')
        if typ != 'OK':
            print("[MAIL] no messages found or search failed:", typ)
            M.logout()
            return results
            ids = data[0].split()
        ids = ids[-max_messages:]
        print(f"[MAIL] found {len(ids)} candidate messages (last {days_back} days).")
        for num in reversed(ids):
            try:
                typ, msg_data = M.fetch(num, '(RFC822)')
                if typ != 'OK':
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                subject = decode_mime_words(msg.get('Subject') or "")
                frm = decode_mime_words(msg.get('From') or "")
                date_str = msg.get('Date')
                try:
                    published = dparser.parse(date_str) if date_str else datetime.now()
                except:
                    published = datetime.now()
                text, links = parse_message_body(msg)
                # Heuristic: if links exist, pick top 6; else extract top headline-like lines
                items = []
                if links:
                    for text_link, href in links[:8]:
                        title = text_link.strip()
                        if not title:
                            title = subject
                        items.append({
                            "title": title,
                            "link": href,
                            "source": frm,
                            "published": published,
                            "context": f"Newsletter: {subject}"
                        })
                else:
                    # Extract candidate headlines from plaintext: lines with capitals or short length
                    lines = [l.strip() for l in text.splitlines() if l.strip()]
                    # pick up to 6 short lines that look like headlines
                    candidates = [l for l in lines if 6 < len(l) < 200][:6]
                    for c in candidates:
                        items.append({
                            "title": c,
                            "link": None,
                            "source": frm,
                            "published": published,
                            "context": f"Newsletter: {subject}"
                        })
                # Mark as seen (optional): uncomment if you want messages marked read
                try:
                    M.store(num, '+FLAGS', '\\Seen')
                except Exception:
                    pass
                # Append items
                for it in items:
                    results.append(it)
            except Exception as e:
                print("[MAIL] parse message error:", e)
                traceback.print_exc()
        M.logout()
    except Exception as e:
        print("[MAIL] IMAP connection error:", e)
        traceback.print_exc()
    return results

def dedupe_items(items):
    seen = set()
    out = []
    for it in items:
        key = (it.get("link") or "") + "|" + it.get("title","")
        key = key.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def build_enriched_items(rss_items, mail_items):
    # mail items get priority — place them first, then RSS
    combined = mail_items + rss_items
    # fetch page text for items missing summary
    enriched = []
    for it in combined:
        title = it.get("title") or ""
        link = it.get("link") or ""
        published = it.get("published") or datetime.now()
        source = it.get("source") or ""
        context = it.get("context") or ""
        content = ""
        if link:
            content = text_from_url(link)
        if not content and it.get("summary"):
            content = it.get("summary")
        # Summarize
        summary = None
        if OPENAI_API_KEY:
            summary = summarize_with_openai(title, content or "", link or "")
        if not summary:
            # fallback compose a readable summary with context
            snippet = content[:600].strip().replace("\n", " ")
            if not snippet:
                snippet = title
                summary = f"{title}\n{snippet}\nSource: {source or context}\nLink: {link or ''}"
        enriched.append({"title": title, "link": link, "summary": summary, "published": published})
    return enriched

def format_message(top_items, want_more):
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    header = f"<b>AI Brief — {date_str}</b>\n\n"
    body = ""
    for i, e in enumerate(top_items, start=1):
        # convert possible HTML summary to plaintext
        s = html2text.html2text(e['summary']) if e.get('summary') else ''
        # keep it concise in message
        body += f"<b>{i}. {e['title']}</b>\n{s}\n\n"
    if want_more:
        body += "<b>Want more (quick links)</b>\n"
        for w in want_more:
            title = w.get('title') or "More"
            link = w.get('link') or ""
            if link:
                body += f"• {title} — {link}\n"
            else:
                body += f"• {title}\n"
    message = header + body
    if len(message) > 3800:
        message = message[:3800] + "\n\n... (truncated)"
    return message

def main():
    print("[START] Digest run:", datetime.now().isoformat())
    # 1) fetch RSS items
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)
    rss_collected = []
    print("[RSS] fetching feeds...")
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
            rss_collected.append({"title": title, "link": link, "summary": summary, "published": published, "source": "RSS"})
    print(f"[RSS] found {len(rss_collected)} recent RSS items.")

    # 2) fetch newsletter items from mailbox
    mail_items = []
    if MAIL_EMAIL and MAIL_APP_PASSWORD:
        print("[MAIL] reading mailbox for newsletters...")
        mail_items = imap_fetch_newsletters(MAIL_EMAIL, MAIL_APP_PASSWORD, days_back=7, max_messages=100)
        print(f"[MAIL] parsed {len(mail_items)} newsletter-derived items.")
    else:
        print("[MAIL] mailbox credentials not set; skipping mailbox read.")

    # 3) dedupe and enrich
    all_items = dedupe_items(mail_items + rss_collected)
    print(f"[STORE] total deduped candidate items: {len(all_items)}")
    enriched = build_enriched_items(rss_collected, mail_items)

    # 4) choose top 5 (mail items prioritized by build_enriched_items ordering)
    top5 = enriched[:5]
    want_more = enriched[5:13]

    # 5) format and post
    message = format_message(top5, want_more)
    try:
        resp = post_telegram(message)
        print("[POST] Telegram post OK:", resp.get("result", {}).get("message_id"))
    except Exception as e:
        print("[POST] Telegram post failed:", e)
        traceback.print_exc()

    print("[END] Digest run completed.")

if __name__ == "__main__":
    main()
