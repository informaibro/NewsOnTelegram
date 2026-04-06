#!/usr/bin/env python3
"""
AI Brief — digest.py
Fase 0 optimizations:
  - Parallel RSS fetching (ThreadPoolExecutor)
  - Parallel GPT summarization
  - Similarity-based deduplication (same story from multiple feeds)
  - Feed quality logging
  - Hard timeouts on all HTTP calls
"""
import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dparser
from openai import OpenAI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env vars.")

MAX_RSS_WORKERS      = 6     # parallel feed fetches
MAX_GPT_WORKERS      = 5     # parallel GPT calls
FEED_TIMEOUT         = 10    # seconds per feed
GPT_MAX_TOKENS       = 220
SIMILARITY_THRESHOLD = 0.72  # titles above this = same story

# ---------------------------------------------------------------------------
# RSS feeds
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    "https://venturebeat.com/category/ai/feed/",
    "https://techcrunch.com/tag/artificial-intelligence/feed/",
    "https://www.theverge.com/ai/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.wired.com/feed/tag/ai/latest/rss",
    "https://openai.com/news/rss.xml",
    "https://deepmind.google/blog/rss/",
    "https://www.anthropic.com/rss.xml",
    "https://www.reuters.com/technology/feed/",
]

AI_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "ml", "llm",
    "large language model", "foundation model", "multimodal", "agent",
    "anthropic", "openai", "chatgpt", "claude", "gpt", "gemini", "mistral",
    "meta ai", "llama", "model", "inference", "training", "generative",
    "prompt", "embedding", "vector database", "edge ai", "ai product",
    "ai startup", "ai funding", "ai regulation", "responsible ai",
    "safety", "alignment", "ai chip", "benchmark", "evaluation",
    "nvidia", "gpu", "tpu", "ai lab", "reasoning model", "context window",
]
AI_KEYWORDS_RE = re.compile(
    r"\b(" + r"|".join(re.escape(k) for k in AI_KEYWORDS) + r")\b",
    flags=re.I,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sanitize_title(raw):
    if not raw:
        return None
    s = str(raw).strip()
    try:
        s = BeautifulSoup(s, "html.parser").get_text(separator=" ", strip=True)
    except Exception:
        pass
    s = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
    s = " ".join(s.split()).strip()
    return s if len(s) >= 2 else None


def is_english(text):
    if not text:
        return False
    sample = text[:500].lower()
    if sum(1 for c in sample if ord(c) > 127) / max(1, len(sample)) > 0.3:
        return False
    return any(w in sample for w in [" the ", " and ", " is ", " for ", " ai ", " model "])


def contains_ai_signal(title, summary=""):
    target = f"{title}\n{summary}"
    if not target.strip() or not is_english(target):
        return False
    return bool(AI_KEYWORDS_RE.search(target))


def title_similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ---------------------------------------------------------------------------
# 1. Parallel RSS fetching
# ---------------------------------------------------------------------------
def fetch_feed_entries(url):
    try:
        d = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        source_name = (getattr(d.feed, "title", None) or url.split("/")[2]).strip()
        out = []
        for e in d.entries:
            title = sanitize_title(getattr(e, "title", "") or "") or "RSS item"
            link  = getattr(e, "link", "") or ""

            summary = ""
            for field in ("summary", "description", "content"):
                val = getattr(e, field, None)
                if val:
                    if isinstance(val, list):
                        val = val[0].get("value", "") if val else ""
                    summary = BeautifulSoup(str(val), "html.parser").get_text(
                        separator=" ", strip=True
                    )
                    summary = " ".join(summary.split())[:1000]
                    if summary:
                        break

            published = None
            for df in ("published", "updated"):
                if hasattr(e, df):
                    try:
                        published = dparser.parse(getattr(e, df))
                        break
                    except Exception:
                        pass

            out.append({
                "title": title, "link": link,
                "summary": summary, "published": published,
                "source": source_name,
            })
        return url, out, source_name, None
    except Exception as exc:
        return url, [], url.split("/")[2], str(exc)


def fetch_all_feeds():
    all_items = []
    quality   = {}

    with ThreadPoolExecutor(max_workers=MAX_RSS_WORKERS) as ex:
        futures = {ex.submit(fetch_feed_entries, url): url for url in RSS_FEEDS}
        for fut in as_completed(futures):
            url, items, source, err = fut.result()
            domain = url.split("/")[2]
            if err:
                print(f"[RSS] ✗ {domain} — {err}")
                quality[domain] = {"items": 0, "with_summary": 0, "error": err}
            else:
                with_summary = sum(1 for i in items if i.get("summary"))
                quality[domain] = {"items": len(items), "with_summary": with_summary}
                print(f"[RSS] ✓ {domain} — {len(items)} items, {with_summary} with summary")
                all_items.extend(items)

    return all_items, quality


# ---------------------------------------------------------------------------
# 2. Similarity-based deduplication
# ---------------------------------------------------------------------------
def dedupe_by_similarity(items):
    # Pass 1 — exact URL
    seen_urls  = set()
    url_deduped = []
    for it in items:
        key = (it.get("link") or "").strip().lower()
        if key and key in seen_urls:
            continue
        if key:
            seen_urls.add(key)
        url_deduped.append(it)

    # Pass 2 — title similarity clusters
    clusters = []
    for it in url_deduped:
        title  = (it.get("title") or "").strip()
        placed = False
        for cluster in clusters:
            if title_similarity(title, cluster[0].get("title", "")) >= SIMILARITY_THRESHOLD:
                cluster.append(it)
                placed = True
                break
        if not placed:
            clusters.append([it])

    result = []
    for cluster in clusters:
        best = max(cluster, key=lambda x: len(x.get("summary") or ""))
        if len(cluster) > 1:
            sources = ", ".join(c.get("source", "?") for c in cluster)
            print(f"[DEDUP] merged {len(cluster)}x → \"{best['title'][:55]}\" ({sources})")
        result.append(best)

    return result


# ---------------------------------------------------------------------------
# 3. Parallel GPT summarization
# ---------------------------------------------------------------------------
_openai_client = None

def get_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


SYSTEM_PROMPT = (
    "You are a senior tech journalist writing an AI news brief for startup founders. "
    "Be concrete and specific — names, numbers, dates. "
    "Never write generic phrases like 'founders should prioritize' or 'this is a notable development'. "
    "If the summary is thin, focus on what the title tells us and what it implies."
)

USER_TEMPLATE = """\
Title: {title}
Source: {source}
URL: {url}

Summary:
{summary}

Write EXACTLY three lines, no extra text:
What happened: <one concrete sentence — the specific fact, product, company, number>
Why it matters: <one sentence — what changes, what's at stake, what's surprising>
Watch: <one sentence — what to follow next, or what this means for AI builders/founders>
"""


def summarize_item(item):
    title   = item.get("title") or ""
    summary = item.get("summary") or ""
    link    = item.get("link") or ""
    source  = item.get("source") or ""

    what = why = watch = None

    if OPENAI_API_KEY and (title or summary):
        try:
            resp = get_client().chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": USER_TEMPLATE.format(
                        title=title, source=source, url=link,
                        summary=summary[:2000],
                    )},
                ],
                max_tokens=GPT_MAX_TOKENS,
                temperature=0.2,
            )
            raw = resp.choices[0].message.content.strip()
            for line in raw.splitlines():
                l = line.strip()
                if l.lower().startswith("what happened:"):
                    what = l[len("what happened:"):].strip()
                elif l.lower().startswith("why it matters:"):
                    why = l[len("why it matters:"):].strip()
                elif l.lower().startswith("watch:"):
                    watch = l[len("watch:"):].strip()
            if not (what and why and watch):
                raise ValueError(f"parse failed: {raw[:60]}")
        except Exception as exc:
            print(f"[GPT] ✗ \"{title[:45]}\": {exc}")

    if not what:
        what  = (summary[:240].rstrip() + "...") if len(summary) > 240 else summary or title
    if not why:
        why   = "Relevant development in the AI ecosystem."
    if not watch:
        watch = "Follow up for more details."

    return {**item, "what_happened": what, "why_important": why, "watch": watch}


def enrich_all(items):
    ai_items = [
        it for it in items
        if contains_ai_signal(it.get("title", ""), it.get("summary", ""))
    ]
    print(f"[FILTER] {len(ai_items)} AI-relevant (from {len(items)} deduped)")

    enriched = []
    with ThreadPoolExecutor(max_workers=MAX_GPT_WORKERS) as ex:
        futures = {ex.submit(summarize_item, it): it for it in ai_items}
        for fut in as_completed(futures):
            try:
                enriched.append(fut.result())
            except Exception as exc:
                print(f"[ENRICH] error: {exc}")

    return enriched


# ---------------------------------------------------------------------------
# 4. Format + Telegram
# ---------------------------------------------------------------------------
SAFE_MAX_LEN = 4000


def _trunc(text, n=300):
    if not text or len(text) <= n:
        return text or ""
    return text[:n - 3].rstrip() + "..."


def format_message(top_items, want_more):
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    body = ""

    for i, e in enumerate(top_items, start=1):
        body += f"{i}. *{e['title']}*"
        if e.get("source"):
            body += f" _({e['source']})_"
        body += "\n"
        body += f"   \U0001f4cc {_trunc(e.get('what_happened'), 300)}\n"
        body += f"   \U0001f4a1 {_trunc(e.get('why_important'), 280)}\n"
        body += f"   \U0001f440 {_trunc(e.get('watch'), 280)}\n"
        if e.get("link"):
            body += f"   \U0001f517 {e['link']}\n"
        body += "\n"

    msg1 = f"\U0001f916 AI Brief \u2014 {date_str}\n\n" + body
    if len(msg1) > SAFE_MAX_LEN:
        msg1 = msg1[:SAFE_MAX_LEN - 30].rstrip() + "\n\n_(continua em Leia mais)_"

    msg2 = None
    if want_more:
        lines = []
        for w in want_more:
            line = f"\u2022 {w.get('title') or 'More'}"
            if w.get("source"):
                line += f" _({w['source']})_"
            if w.get("link"):
                line += f"\n  {w['link']}"
            lines.append(line)
        msg2 = f"\U0001f4ce Leia mais \u2014 AI Brief {date_str}\n\n" + "\n".join(lines)
        if len(msg2) > SAFE_MAX_LEN:
            msg2 = msg2[:SAFE_MAX_LEN - 20].rstrip() + "\n\n_(truncado)_"

    return msg1, msg2


def post_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    t0 = datetime.now()
    print(f"[START] {t0.isoformat()}")

    raw_items, quality = fetch_all_feeds()
    print(f"[RSS] {len(raw_items)} raw items total")

    low_q = [d for d, s in quality.items() if s.get("with_summary", 0) == 0 and s.get("items", 0) > 0]
    if low_q:
        print(f"[QUALITY] zero-summary feeds (consider replacing): {', '.join(low_q)}")

    deduped = dedupe_by_similarity(raw_items)
    print(f"[DEDUP] {len(deduped)} unique items (merged {len(raw_items) - len(deduped)} dupes)")

    enriched = enrich_all(deduped)

    enriched_sorted = sorted(
        enriched,
        key=lambda x: x.get("published") or datetime.now(timezone.utc),
        reverse=True,
    )

    top5      = enriched_sorted[:5]
    want_more = enriched_sorted[5:13]

    if not top5:
        print("[WARN] No AI items — skipping Telegram.")
        return

    msg1, msg2 = format_message(top5, want_more)

    try:
        r1 = post_telegram(msg1)
        print(f"[POST] principal OK (id={r1.get('result', {}).get('message_id')})")
        if msg2:
            r2 = post_telegram(msg2)
            print(f"[POST] leia mais OK (id={r2.get('result', {}).get('message_id')})")
    except Exception as exc:
        print(f"[POST] failed: {exc}")
        traceback.print_exc()

    elapsed = (datetime.now() - t0).seconds
    print(f"[END] concluido em {elapsed}s")


if __name__ == "__main__":
    main()
