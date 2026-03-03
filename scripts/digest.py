Relevance & enrichment

--------------------

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
try:
import openai
openai.api_key = OPENAI_API_KEY
prompt = (
f"Write a concise, non-technical 50-70 word summary (3-4 short sentences) for the following news item. "
f"Include: what happened, who (company/people), context/implication for founders/entrepreneurs, and one short 'Founder action' line. "
f"Keep it plain English and focus on business/market impact. Include title and url.\n\n"
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

def simple_extract_summary(title, content, url):
lines = [l.strip() for l in content.splitlines() if l.strip()]
snippet = " ".join(lines[:4]) if lines else title
action = "Founder action: monitor vendor integrations and run a small 30-day pilot with defined KPIs."
return f"{title}\n{snippet}\n{action}\n{url}"

def build_enriched_items(rss_items, mail_items):
mail_ai = [m for m in mail_items if contains_ai_signal(m.get("title", "") + " " + (m.get("context", "") or ""))]
rss_ai = [r for r in rss_items if contains_ai_signal(r.get("title", "") + " " + (r.get("summary", "") or ""))]
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
if OPENAI_API_KEY:
summary = summarize_with_openai(title, content or "", link or "")
if not summary:
summary = simple_extract_summary(title, content or "", link or "")
enriched.append({
"title": title,
"link": link,
"summary": summary,
"published": published
})
return enriched

--------------------

Formatting & posting

--------------------

def format_message(top_items, want_more):
date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
header = f"<b>AI Brief — {date_str}</b>\n\n"
body = ""
for i, e in enumerate(top_items, start=1):
s = ""
if e.get('summary'):
try:
s = html2text.html2text(e['summary'])
except Exception:
s = str(e.get('summary'))
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

--------------------

Telegram post helper

--------------------

def post_telegram(text):
api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
try:
r = requests.post(api, json=payload, timeout=20)
r.raise_for_status()
return r.json()
except Exception as e:
print("[POST] Telegram error:", e)
raise

--------------------

Main

--------------------

def main():
print("[START] Digest run:", datetime.now().isoformat())
now = datetime.now(timezone.utc)


# Collect RSS / scrapes
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

# Mailbox
mail_items = []
if MAIL_EMAIL and MAIL_APP_PASSWORD:
    print("[MAIL] reading mailbox for newsletters...")
    mail_items = imap_fetch_newsletters(MAIL_EMAIL, MAIL_APP_PASSWORD, days_back=7, max_messages=200)
    print(f"[MAIL] parsed {len(mail_items)} newsletter-derived items.")
else:
    print("[MAIL] mailbox credentials not set; skipping mailbox read.")

# Dedupe
all_candidates = dedupe_items(mail_items + rss_collected)
print(f"[STORE] candidates after dedupe: {len(all_candidates)}")

# Enrich and filter by AI relevance
enriched = build_enriched_items(rss_collected, mail_items)
print(f"[ENRICH] enriched AI-relevant items count: {len(enriched)}")

# Choose top 5 by published date
enriched_sorted = sorted(enriched, key=lambda x: x.get("published") or datetime.now(), reverse=True)
top5 = enriched_sorted[:5]
want_more = enriched_sorted[5:13]

# Format and post
message = format_message(top5, want_more)
try:
    resp = post_telegram(message)
    print("[POST] Telegram post OK:", resp.get("result", {}).get("message_id"))
except Exception as e:
    print("[POST] Telegram post failed:", e)
    traceback.print_exc()

print("[END] Digest run completed.")
if name == "main":
main()...
