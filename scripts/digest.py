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

def main():
print("[START] Digest run:", datetime.now().isoformat())
now = datetime.now(timezone.utc)
since = now - timedelta(days=1)
rss_collected = []
print("[RSS] fetching configured feeds...")
for feed in RSS_FEEDS:
try:
if "treeofalpha" in feed:
scraped = scrape_tree_of_alpha_latest()
rss_collected.extend(scraped)
else:
entries = fetch_feed_entries(feed)
for e in entries:
if e.get("published") and isinstance(e.get("published"), str):
try:
e["published"] = dparser.parse(e["published"])
except:
pass
rss_collected.append(e)
except Exception as e:
print("[RSS] feed error", feed, e)
print(f"[RSS] gathered {len(rss_collected)} rss/scrape items (pre-filter).")
mail_items = []
if MAIL_EMAIL and MAIL_APP_PASSWORD:
    print("[MAIL] reading mailbox for newsletters...")
    mail_items = imap_fetch_newsletters(MAIL_EMAIL, MAIL_APP_PASSWORD, days_back=7, max_messages=200)
    print(f"[MAIL] parsed {len(mail_items)} newsletter-derived items.")
else:
    print("[MAIL] mailbox credentials not set; skipping mailbox read.")

all_candidates = dedupe_items(mail_items + rss_collected)
print(f"[STORE] candidates after dedupe: {len(all_candidates)}")
enriched = build_enriched_items(rss_collected, mail_items)
print(f"[ENRICH] enriched AI-relevant items count: {len(enriched)}")

enriched_sorted = sorted(enriched, key=lambda x: x.get("published") or datetime.now(), reverse=True)
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
if name == "main":
main()
