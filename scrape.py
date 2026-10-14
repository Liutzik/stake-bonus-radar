"""Collects bonus announcements from Stake's official public Telegram channels.

Reads the public web preview (t.me/s/<channel>), which needs no login. The first
run backfills history; later runs only fetch pages newer than what is stored.
"""
import html
import json
import re
import time
import urllib.request
from pathlib import Path

DATA = Path(__file__).parent / "data"
CHANNELS = {"stake.com": "StakeCasino", "stake.us": "StakeSocial"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
BACKFILL_UNTIL = "2023-01-01"  # oldest post date to walk back to on the first run
BONUS_WORDS = re.compile(r"(?i)monthly|weekly bonus|pre[\s-]*month|post[\s-]*month")


def fetch(channel, before=None):
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "ignore")


def parse(page):
    posts = []
    for chunk in page.split('<div class="tgme_widget_message_wrap')[1:]:
        pid = re.search(r'data-post="[^/"]+/(\d+)"', chunk)
        when = re.search(r'<time datetime="([^"]+)"', chunk)
        body = re.search(r'(?s)<div class="tgme_widget_message_text[^>]*>(.*?)</div>', chunk)
        if not (pid and when):
            continue
        text = ""
        if body:
            text = re.sub(r"<br\s*/?>", "\n", body.group(1))
            text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        posts.append({"id": int(pid.group(1)), "time": when.group(1), "text": text})
    return posts


def update_channel(site, channel, log=print):
    path = DATA / f"posts_{channel}.json"
    store = json.loads(path.read_text("utf-8")) if path.exists() else {"newest_id": 0, "posts": []}
    known = {p["id"] for p in store["posts"]}
    newest = store["newest_id"]
    backfill = newest == 0

    before, added, pages = None, 0, 0
    while True:
        page = parse(fetch(channel, before))
        pages += 1
        if not page:
            break
        for p in page:
            if p["id"] not in known and BONUS_WORDS.search(p["text"].split("\n", 1)[0] + " " + p["text"][:200]):
                store["posts"].append(p)
                known.add(p["id"])
                added += 1
        oldest = min(p["id"] for p in page)
        store["newest_id"] = max(store["newest_id"], max(p["id"] for p in page))
        done_new = not backfill and oldest <= newest
        done_old = backfill and min(p["time"] for p in page) < BACKFILL_UNTIL
        if done_new or done_old or oldest <= 1:
            break
        before = oldest
        time.sleep(0.4)  # be polite to Telegram

    store["posts"].sort(key=lambda p: p["id"])
    DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(store, ensure_ascii=False, indent=1), "utf-8")
    log(f"{site}: {pages} page(s), {added} new bonus post(s)")
    return added


def update_all(log=print):
    total = 0
    for site, channel in CHANNELS.items():
        try:
            total += update_channel(site, channel, log)
        except Exception as e:  # offline or Telegram hiccup: keep the stored data
            log(f"{site}: fetch failed ({e}); using stored data")
    return total


if __name__ == "__main__":
    update_all()
