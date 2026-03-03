#!/usr/bin/env python3
import os
import requests
import feedparser
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta, timezone
from dateutil import parser as dparser
from bs4 import BeautifulSoup
import html2text
import re
import traceback
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MAIL_EMAIL = os.getenv("MAIL_EMAIL")
MAIL_APP_PASSWORD = os.getenv("MAIL_APP_PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER") or "imap.gmail.com"
IMAP_PORT = int(os.getenv("IMAP_PORT") or "993")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit(
        "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID environment variables (set as GitHub secrets)."
    )

AI_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "ml", "llm", "large language model",
    "foundation model", "multimodal", "agent", "anthropic", "openai", "chatgpt", "claude", "gpt",
    "model", "inference", "training", "generative", "prompt", "embedding",
    "vector database", "deployment", "edge ai", "ai product", "ai startup", "ai funding",
    "ai partnership", "ai regulation", "responsible ai", "safety", "alignment", "ai chip",
    "inference cost", "benchmark", "evaluation", "ethics"
]
AI_KEYWORDS_RE = re.compile(
    r"\b(" + r"|".join([re.escape(k) for k in AI_KEYWORDS]) + r")\b",
    flags=re.I
)

RSS_FEEDS = [
    "https://news.treeofalpha.com/feed.xml",
    "https://artificialintelligence-news.com/feed/",
    "https://openai.com/blog/rss",
    "https://deepmind.com/blog/rss.xml",
    "https://www.theverge.com/ai/rss/index.xml",
    "https://techcrunch.com/tag/artificial-intelligence/feed/",
    "https://www.reuters.com/technology/feed/",
]


def fetch_feed_entries(url):
    try:
        d = feedparser.parse(url)
        entries = d.entries if "entries" in d else []
        out = []
        for e in entries:
            title = getattr(e, "title", "") or ""
            link = getattr(e, "link", "") or ""
            summary = getattr(e, "summary", "") or ""
            published = None

            if hasattr(e, "published"):
                try:
                    published = dparser.parse(e.published)
                except Exception:
                    published = None

            if not published and hasattr(e, "updated"):
                try:
                    published = dparser.parse(e.updated)
                except Exception:
                    published = None

            out.append(
                {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published": published,
                    "source": "RSS",
                }
            )
        return out
    except Exception as e:
        print("[RSS] error parsing feed", url, e)
        return []


def text_from_url(url):
    try:
        r = requests.get(
            url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for s in soup(["script", "style", "noscript"]):
            s.decompose()
        return soup.get_text(separator="\n").strip()
    except Exception as e:
        print("[FETCH] error fetching", url, e)
        return ""


def scrape_tree_of_alpha_latest():
    url = "https://news.treeofalpha.com/"
    try:
        r = requests.get(
            url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        results = []

        for a in soup.select("article a[href]"):
            href = a.get("href", "").strip()
            title = a.get_text(strip=True)
            if not href or not title:
                continue
            if href.startswith("/"):
                href = "https://news.treeofalpha.com" + href
            results.append(
                {
                    "title": title,
                    "link": href,
                    "published": datetime.now(timezone.utc),
                    "source": "TreeOfAlpha",
                }
            )

        seen = set()
        uniq = []
        for ritem in results:
            k = (ritem.get("link") or ritem.get("title")).strip()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(ritem)

        return uniq[:12]
    except Exception as e:
        print("[SCRAPE] TreeOfAlpha error:", e)
        return []


def decode_mime_words(s):
    try:
        parts = decode_header(s)
        out = []
        for part, enc in parts:
            if isinstance(part, bytes):
                out.append(part.decode(enc or "utf-8", errors="ignore"))
            else:
                out.append(part)
        return "".join(out)
    except Exception:
        return s


def extract_links_from_html(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(separator=" ", strip=True)
        if href and text:
            links.append((text, href))
    return links


def parse_message_body(msg):
    body = ""
    html = None

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/html" and "attachment" not in disp:
                html = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                try:
                    html = html.decode(charset, errors="ignore")
                except Exception:
                    html = html.decode("utf-8", errors="ignore")
                break

        if not html:
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    text = part.get_payload(decode=True)
                    try:
                        text = text.decode(
                            part.get_content_charset() or "utf-8",
                            errors="ignore",
                        )
                    except Exception:
                        text = text.decode("utf-8", errors="ignore")
                    body = text
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                body = payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="ignore",
                )
            except Exception:
                body = payload.decode("utf-8", errors="ignore")

    if html:
        links = extract_links_from_html(html)
        text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
        return text, links

    return body, []


def imap_fetch_newsletters(email_addr, app_password, days_back=7, max_messages=100):
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
        typ, data = M.search(None, f"(OR UNSEEN SINCE {since_date})")
        if typ != "OK":
            print("[MAIL] no messages found or search failed:", typ)
            M.logout()
            return results

        ids = data[0].split()
        ids = ids[-max_messages:]
        print(f"[MAIL] found {len(ids)} candidate messages (last {days_back} days).")

        for num in reversed(ids):
            try:
                typ, msg_data = M.fetch(num, "(RFC822)")
                if typ != "OK":
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                subject = decode_mime_words(msg.get("Subject") or "")
                frm = decode_mime_words(msg.get("From") or "")
                date_str = msg.get("Date")

                try:
                    published = dparser.parse(date_str) if date_str else datetime.now()
                except Exception:
                    published = datetime.now()

                text, links = parse_message_body(msg)
                items = []

                if links:
                    for text_link, href in links[:8]:
                        title = text_link.strip() or subject
                        items.append(
                            {
                                "title": title,
                                "link": href,
                                "source": frm,
                                "published": published,
                                "context": f"Newsletter: {subject}",
                            }
                        )
                else:
                    lines = [l.strip() for l in text.splitlines() if l.strip()]
                    candidates = [l for l in lines if 6 < len(l) < 200][:6]
                    for c in candidates:
                        items.append(
                            {
                                "title": c,
                                "link": None,
                                "source": frm,
                                "published": published,
                                "context": f"Newsletter: {subject}",
                            }
                        )

                try:
                    M.store(num, "+FLAGS", "\\Seen")
                except Exception:
                    pass

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
        key = (it.get("link") or "") + "|" + it.get("title", "")
        key = key.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def is_english(text):
    if not text:
        return False
    sample = text[:500].lower()
    common = [" the ", " and ", " is ", " for ", " ai ", " model ", " company ", " product "]
    if sum(1 for c in sample if ord(c) > 127) / max(1, len(sample)) > 0.3:
        return False
    return any(w in sample for w in common)


def contains_ai_signal(text, title=""):
    target = (title or "") + "\n" + (text or "")
    if not target:
        return False
    if not is_english(target):
        return False
    return bool(AI_KEYWORDS_RE.search(target))


def summarize_with_openai(title, content, url):
    if not OPENAI_API_KEY:
        return None

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        prompt = (
            "You are an AI news brief assistant for startup founders.\n"
            "Given the title, URL, and content of a news item, produce EXACTLY three lines in plain English:\n"
            "1) Summary: one concise sentence about what happened.\n"
            "2) Why it matters: one sentence explaining why this is strategically important.\n"
            "3) Impact: one sentence about concrete implications or actions for founders/operators.\n\n"
            "Rules:\n"
            "- Output MUST be exactly three lines, in this format:\n"
            "  Summary: ...\n"
            "  Why it matters: ...\n"
            "  Impact: ...\n"
            "- No markdown, no bullet points, no extra text.\n"
        )

        full = f"Title: {title}\nURL: {url}\n\nContent:\n{content[:8000]}"

        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt + "\n\n" + full}],
            max_tokens=220,
            temperature=0.2,
        )

        raw = resp.choices[0].message.content.strip()
        summary = ""
        why = ""
        impact = ""

        for line in raw.splitlines():
            line = line.strip()
            if line.lower().startswith("summary:"):
                summary = line[len("summary:"):].strip()
            elif line.lower().startswith("why it matters:"):
                why = line[len("why it matters:"):].strip()
            elif line.lower().startswith("impact:"):
                impact = line[len("impact:"):].strip()

        if not (summary and why and impact):
            raise ValueError("Could not parse three-line summary from OpenAI output")

        return summary, why, impact

    except Exception as e:
        print("[OpenAI] error:", e)
        return None


def simple_extract_summary(title, content, url):
    lines = [l.strip() for l in (content or "").splitlines() if l.strip()]
    snippet = " ".join(lines[:4]) if lines else title
    if len(snippet) > 260:
        snippet = snippet[:260] + "..."

    summary = snippet or title
    why = (
        "This is a notable development in the AI ecosystem that could affect product, strategy, or competition."
    )
    impact = (
        "Founders should consider how this changes their roadmap, differentiation, or go-to-market over the next 3–6 months."
    )
    return summary, why, impact


def build_enriched_items(rss_items, mail_items):
    mail_ai = [
        m
        for m in mail_items
        if contains_ai_signal(m.get("title", "") + " " + (m.get("context", "") or ""))
    ]
    rss_ai = [
        r
        for r in rss_items
        if contains_ai_signal(r.get("title", "") + " " + (r.get("summary", "") or ""))
    ]

    combined = mail_ai + rss_ai
    enriched = []

    for it in combined:
        title = it.get("title") or ""
        link = it.get("link") or ""
        published = it.get("published") or datetime.now()

        content = ""
        if link:
            content = text_from_url(link)
        if not content and it.get("summary"):
            content = it.get("summary")

        summary = None
        why = None
        impact = None

        if OPENAI_API_KEY:
            res = summarize_with_openai(title, content or "", link or "")
            if res is not None:
                summary, why, impact = res

        if not (summary and why and impact):
            summary, why, impact = simple_extract_summary(title, content or "", link or "")

        enriched.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "why_important": why,
                "impact": impact,
                "published": published,
            }
        )

    return enriched


def format_message(top_items, want_more):
    # ex.: "AI Brief — 2026-03-03"
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    header = f"AI Brief — {date_str}\n\n"
    body = ""

    # Top 5
    for i, e in enumerate(top_items, start=1):
        title = e["title"]
        link = e.get("link") or ""
        summary = e.get("summary") or ""
        why = e.get("why_important") or ""
        impact = e.get("impact") or ""

        body += f"{i}. *{title}*\n"
        body += f"   Summary: {summary}\n"
        body += f"   Why it matters: {why}\n"
        body += f"   Impact for founders: {impact}\n"
        if link:
            body += f"   Link: {link}\n"
        body += "\n"

    # Seção "Want more" / aprofundar
    if want_more:
        body += "More to explore (if you want to go deeper):\n"
        for w in want_more:
            w_title = w.get("title") or "More"
            w_link = w.get("link") or ""
            if w_link:
                body += f"- {w_title} — {w_link}\n"
            else:
                body += f"- {w_title}\n"

    message = header + body
    if len(message) > 3800:
        message = message[:3800] + "\n\n... (truncated)"
    return message


def post_telegram(text):
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(api, json=payload, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[POST] Telegram error:", e)
        raise


def main():
    print("[START] Digest run:", datetime.now().isoformat())
    now = datetime.now(timezone.utc)

    rss_collected = []
    print("[RSS] fetching configured feeds...")
    for feed in RSS_FEEDS:
        try:
            if "treeofalpha" in feed:
                scraped = scrape_tree_of_alpha_latest()
                rss_collected.extend(scraped)
            else:
                entries = fetch_feed_entries(feed)
                rss_collected.extend(entries)
        except Exception as e:
            print("[RSS] feed error", feed, e)

    print(f"[RSS] gathered {len(rss_collected)} rss/scrape items (pre-filter).")

    mail_items = []
    if MAIL_EMAIL and MAIL_APP_PASSWORD:
        print("[MAIL] reading mailbox for newsletters...")
        mail_items = imap_fetch_newsletters(
            MAIL_EMAIL,
            MAIL_APP_PASSWORD,
            days_back=7,
            max_messages=200,
        )
        print(f"[MAIL] parsed {len(mail_items)} newsletter-derived items.")
    else:
        print("[MAIL] mailbox credentials not set; skipping mailbox read.")

    all_candidates = dedupe_items(mail_items + rss_collected)
    print(f"[STORE] candidates after dedupe: {len(all_candidates)}")

    enriched = build_enriched_items(rss_collected, mail_items)
    print(f"[ENRICH] enriched AI-relevant items count: {len(enriched)}")

    enriched_sorted = sorted(
        enriched,
        key=lambda x: x.get("published") or datetime.now(),
        reverse=True,
    )
    top5 = enriched_sorted[:5]
    want_more = enriched_sorted[5:13]

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
