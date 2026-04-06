#!/usr/bin/env python3
import os
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from dateutil import parser as dparser
from bs4 import BeautifulSoup
import re
import traceback
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit(
        "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID environment variables (set as GitHub secrets)."
    )

AI_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "ml", "llm", "large language model",
    "foundation model", "multimodal", "agent", "anthropic", "openai", "chatgpt", "claude", "gpt",
    "gemini", "mistral", "meta ai", "llama", "model", "inference", "training", "generative",
    "prompt", "embedding", "vector database", "deployment", "edge ai", "ai product",
    "ai startup", "ai funding", "ai partnership", "ai regulation", "responsible ai",
    "safety", "alignment", "ai chip", "inference cost", "benchmark", "evaluation",
    "nvidia", "gpu", "tpu", "ai lab", "reasoning model", "context window",
]
AI_KEYWORDS_RE = re.compile(
    r"\b(" + r"|".join([re.escape(k) for k in AI_KEYWORDS]) + r")\b",
    flags=re.I
)

# ---------------------------------------------------------------------------
# RSS FEEDS — fontes primárias de alta qualidade, sem newsletters de email
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    # Notícias primárias — breaking news e lançamentos
    "https://venturebeat.com/category/ai/feed/",
    "https://techcrunch.com/tag/artificial-intelligence/feed/",
    "https://www.theverge.com/ai/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.wired.com/feed/tag/ai/latest/rss",

    # Fontes oficiais dos labs — lançamentos diretos
    "https://openai.com/news/rss.xml",
    "https://deepmind.google/blog/rss/",
    "https://www.anthropic.com/rss.xml",

    # Negócios e mercado
    "https://www.reuters.com/technology/feed/",
]


def sanitize_title(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        soup = BeautifulSoup(s, "html.parser")
        out = soup.get_text(separator=" ", strip=True)
    except Exception:
        out = s
    out = re.sub(r"<!--.*?-->", "", out, flags=re.DOTALL)
    out = " ".join(out.split()).strip()
    if not out or len(out) < 2:
        return None
    return out


def fetch_feed_entries(url):
    try:
        d = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        entries = d.entries if "entries" in d else []
        source_name = (d.feed.get("title") or url.split("/")[2]).strip()
        out = []
        for e in entries:
            raw_title = getattr(e, "title", "") or ""
            title = sanitize_title(raw_title) or "RSS item"
            link = getattr(e, "link", "") or ""

            # Pegar summary do RSS — já é suficiente, sem scraping de página
            summary = ""
            for field in ("summary", "description", "content"):
                val = getattr(e, field, None)
                if val:
                    if isinstance(val, list):
                        val = val[0].get("value", "") if val else ""
                    summary = BeautifulSoup(str(val), "html.parser").get_text(separator=" ", strip=True)
                    summary = " ".join(summary.split())[:1000]
                    if summary:
                        break

            published = None
            for date_field in ("published", "updated"):
                if hasattr(e, date_field):
                    try:
                        published = dparser.parse(getattr(e, date_field))
                        break
                    except Exception:
                        pass

            out.append({
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
                "source": source_name,
            })
        return out
    except Exception as e:
        print("[RSS] error parsing feed", url, e)
        return []


def dedupe_items(items):
    seen = set()
    out = []
    for it in items:
        key = (it.get("link") or it.get("title", "")).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def is_english(text):
    if not text:
        return False
    sample = text[:500].lower()
    if sum(1 for c in sample if ord(c) > 127) / max(1, len(sample)) > 0.3:
        return False
    common = [" the ", " and ", " is ", " for ", " ai ", " model ", " company ", " new "]
    return any(w in sample for w in common)


def contains_ai_signal(text, title=""):
    target = (title or "") + "\n" + (text or "")
    if not target.strip():
        return False
    if not is_english(target):
        return False
    return bool(AI_KEYWORDS_RE.search(target))


def summarize_with_openai(title, summary, url, source):
    if not OPENAI_API_KEY:
        return None
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        prompt = (
            "You are an AI news curator for a Brazilian tech newsletter. "
            "Your job is to write tight, concrete, factual briefs — like a senior journalist, not a consultant. "
            "Avoid generic advice. Be specific to THIS story.\n\n"
            "Given the title, source, URL and summary of a news item, produce EXACTLY three lines:\n"
            "1) What happened: one direct sentence about the concrete fact (new model, acquisition, funding, feature launch, etc.)\n"
            "2) Why it matters: one sentence with specific context — what changes, what's at stake, what's surprising.\n"
            "3) Watch: one sentence on what to watch next or what this means for AI builders/founders specifically.\n\n"
            "Rules:\n"
            "- Output MUST be exactly three lines in this format:\n"
            "  What happened: ...\n"
            "  Why it matters: ...\n"
            "  Watch: ...\n"
            "- Be specific: mention company names, model names, dollar amounts, dates — whatever makes it concrete.\n"
            "- No generic phrases like 'founders should prioritize' or 'this is a notable development'.\n"
            "- No markdown, no bullet points, no extra text.\n"
        )

        content = f"Title: {title}\nSource: {source}\nURL: {url}\n\nSummary:\n{summary[:2000]}"

        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt + "\n\n" + content}],
            max_tokens=220,
            temperature=0.2,
        )

        raw = resp.choices[0].message.content.strip()
        what = why = watch = ""

        for line in raw.splitlines():
            line = line.strip()
            if line.lower().startswith("what happened:"):
                what = line[len("what happened:"):].strip()
            elif line.lower().startswith("why it matters:"):
                why = line[len("why it matters:"):].strip()
            elif line.lower().startswith("watch:"):
                watch = line[len("watch:"):].strip()

        if not (what and why and watch):
            raise ValueError("Could not parse three-line summary from OpenAI output")

        return what, why, watch

    except Exception as e:
        print("[OpenAI] error:", e)
        return None


def simple_extract_summary(title, summary):
    what = summary[:240].rstrip() + "..." if len(summary) > 240 else summary or title
    why = "Relevant development in the AI ecosystem."
    watch = "Monitor follow-up coverage for more details."
    return what, why, watch


def build_enriched_items(rss_items):
    ai_items = [
        r for r in rss_items
        if contains_ai_signal(r.get("title", "") + " " + (r.get("summary", "") or ""))
    ]

    enriched = []
    for it in ai_items:
        title = sanitize_title(it.get("title") or "") or "Untitled"
        link = it.get("link") or ""
        summary = it.get("summary") or ""
        source = it.get("source") or ""
        published = it.get("published") or datetime.now(timezone.utc)

        # Sem scraping — usa só o summary do RSS
        res = None
        if OPENAI_API_KEY and (summary or title):
            res = summarize_with_openai(title, summary, link, source)

        if res:
            what, why, watch = res
        else:
            what, why, watch = simple_extract_summary(title, summary)

        enriched.append({
            "title": title,
            "link": link,
            "what_happened": what,
            "why_important": why,
            "watch": watch,
            "published": published,
            "source": source,
        })

    return enriched


SAFE_MAX_LEN = 4000


def _truncate_field(text, max_len=300):
    if not text or len(text) <= max_len:
        return text or ""
    return text[:max_len - 3].rstrip() + "..."


def format_message(top_items, want_more):
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    header = f"🤖 AI Brief — {date_str}\n\n"
    body = ""

    for i, e in enumerate(top_items, start=1):
        title = e["title"]
        link = e.get("link") or ""
        source = e.get("source") or ""
        what = _truncate_field(e.get("what_happened") or "", 300)
        why = _truncate_field(e.get("why_important") or "", 280)
        watch = _truncate_field(e.get("watch") or "", 280)

        body += f"{i}. *{title}*"
        if source:
            body += f" _({source})_"
        body += "\n"
        body += f"   📌 {what}\n"
        body += f"   💡 {why}\n"
        body += f"   👀 {watch}\n"
        if link:
            body += f"   🔗 {link}\n"
        body += "\n"

    msg_principal = header + body
    if len(msg_principal) > SAFE_MAX_LEN:
        msg_principal = msg_principal[:SAFE_MAX_LEN - 20].rstrip() + "\n\n_(continua em Leia mais)_"

    msg_leia_mais = None
    if want_more:
        header_more = f"📎 Leia mais — AI Brief {date_str}\n\n"
        lines = []
        for w in want_more:
            w_title = w.get("title") or "More"
            w_link = w.get("link") or ""
            w_source = w.get("source") or ""
            line = f"• {w_title}"
            if w_source:
                line += f" _({w_source})_"
            if w_link:
                line += f"\n  {w_link}"
            lines.append(line)
        msg_leia_mais = header_more + "\n".join(lines)
        if len(msg_leia_mais) > SAFE_MAX_LEN:
            msg_leia_mais = msg_leia_mais[:SAFE_MAX_LEN - 20].rstrip() + "\n\n_(truncado)_"

    return msg_principal, msg_leia_mais


def post_telegram(text):
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
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

    rss_collected = []
    print("[RSS] fetching feeds...")
    for feed_url in RSS_FEEDS:
        try:
            entries = fetch_feed_entries(feed_url)
            print(f"[RSS] {feed_url.split('/')[2]} — {len(entries)} items")
            rss_collected.extend(entries)
        except Exception as e:
            print("[RSS] feed error", feed_url, e)

    print(f"[RSS] total raw items: {len(rss_collected)}")

    all_candidates = dedupe_items(rss_collected)
    print(f"[DEDUP] after dedupe: {len(all_candidates)}")

    enriched = build_enriched_items(all_candidates)
    print(f"[ENRICH] AI-relevant items: {len(enriched)}")

    # Ordenar por data mais recente
    enriched_sorted = sorted(
        enriched,
        key=lambda x: x.get("published") or datetime.now(timezone.utc),
        reverse=True,
    )

    top5 = enriched_sorted[:5]
    want_more = enriched_sorted[5:13]

    if not top5:
        print("[WARN] No AI items found today. Skipping Telegram post.")
        return

    msg_principal, msg_leia_mais = format_message(top5, want_more)

    try:
        resp = post_telegram(msg_principal)
        print("[POST] Telegram principal OK:", resp.get("result", {}).get("message_id"))
        if msg_leia_mais:
            resp2 = post_telegram(msg_leia_mais)
            print("[POST] Telegram Leia mais OK:", resp2.get("result", {}).get("message_id"))
    except Exception as e:
        print("[POST] Telegram post failed:", e)
        traceback.print_exc()

    print("[END] Digest run completed.")


if __name__ == "__main__":
    main()
