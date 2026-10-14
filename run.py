"""Refresh the Stake bonus radar: fetch new announcements, re-predict, rebuild the page.

  python run.py          refresh and print a summary
  python run.py --open   refresh and open the dashboard
  python run.py --hook   for Claude's SessionStart hook: refresh, open the dashboard
                         (at most once every few hours), and report back as hook JSON
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import model
import scrape
import traffic

HERE = Path(__file__).parent
PAGE = HERE / "dashboard.html"
SHARE = HERE / "share.html"
OPENED = HERE / "data" / "last_opened"
REOPEN_AFTER = 3 * 3600  # seconds; several Claude windows opening together get one tab
LABELS = {"monthly": "monthly", "pre": "pre-monthly", "post": "post-monthly"}


def local(t):
    return t.astimezone().strftime("%a %d %b %H:%M")


def peak_local_day(pred, now):
    """The local calendar day holding the most remaining chance (what the dashboard shows)."""
    days = {}
    for t, p in pred["slots"]:
        if t >= now:
            d = t.astimezone().date()
            days[d] = days.get(d, 0) + p
    return max(days, key=days.get)


def busiest_hour(site, events, now):
    """The forecast's busiest hour in the next 7 days, for the one-line summary."""
    preds = {}
    for kind in ("monthly", "pre", "post"):
        pred = model.next_target(events, kind, now)
        if pred:
            preds[kind] = dict(pred, summary=model.summarize(pred, now))
    t = traffic.analyse(site, events, preds, now)
    if not t or not t["forecast"]:
        return None
    week = [f for f in t["forecast"] if f[0] < now.timestamp() + 7 * 86400]
    ts, players, lift = max(week, key=lambda f: f[1])
    why = " (bonus day)" if lift > 1.1 else ""
    return f"busiest hour this week {local(datetime.fromtimestamp(ts, timezone.utc))} ~{players:,} players{why}"


def summary_lines(now):
    lines = []
    for site in model.SITES:
        events = model.load_events(site)
        parts = []
        for kind in ("monthly", "pre", "post"):
            pred = model.next_target(events, kind, now)
            if not pred:
                continue
            s = model.summarize(pred, now)
            if s["chance"] < 0.03:
                continue
            lo, hi = s["w80"]
            chance = "" if kind == "monthly" else f" {s['chance']:.0%} chance,"
            parts.append(f"{LABELS[kind]}{chance} likely {peak_local_day(pred, now):%a %d %b} "
                         f"(80%: {lo.astimezone():%d %b}-{hi.astimezone():%d %b})")
        parts.append(f"weekly {local(model.next_weekly(now))}")
        busy = busiest_hour(site, events, now)
        if busy:
            parts.append(busy)
        lines.append(f"{site}: " + "; ".join(parts))
    return lines


def page_data(now):
    """Everything the page shows: every bonus type, without the player counts."""
    data = model.build(now)
    for site_data in data["sites"].values():
        site_data.pop("traffic", None)
    return data


def render(data, wrapper=True):
    page = (HERE / "template.html").read_text("utf-8").replace("/*DATA*/null", json.dumps(data, separators=(",", ":")))
    if not wrapper:
        # the artifact host adds the document wrapper itself
        for tag in ("<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
                    '<meta name="viewport" content="width=device-width, initial-scale=1">', "</head>", "<body>", "</body>", "</html>"):
            page = page.replace(tag, "", 1)
    return page


def refresh(log):
    added = scrape.update_all(log)
    traffic.update(log)
    now = datetime.now(timezone.utc)
    data = page_data(now)
    PAGE.write_text(render(data), "utf-8")
    SHARE.write_text(render(data, wrapper=False), "utf-8")
    return added, now


def should_open():
    try:
        return time.time() - OPENED.stat().st_mtime > REOPEN_AFTER
    except OSError:
        return True


def open_page():
    OPENED.parent.mkdir(exist_ok=True)
    OPENED.write_text(datetime.now().isoformat(), "utf-8")
    os.startfile(PAGE)  # Windows: opens in the default browser


def main():
    hook = "--hook" in sys.argv
    notes = []
    added, now = refresh(notes.append if hook else print)
    lines = summary_lines(now)
    if hook:
        if should_open():
            open_page()
        new = f" ({added} new announcement{'s' * (added != 1)})" if added else ""
        msg = "Stake bonus radar" + new + "\n" + "\n".join(lines)
        print(json.dumps({
            "systemMessage": msg,
            "hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": msg + f"\nDashboard: {PAGE}"},
        }))
        return
    print("\n".join(lines))
    print(f"Dashboard: {PAGE}")
    if "--open" in sys.argv:
        open_page()


if __name__ == "__main__":
    main()
