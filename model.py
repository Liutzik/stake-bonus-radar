"""Turns Stake's bonus announcements into timing predictions.

Every announcement becomes an event: a type (monthly / pre / post / extra), the
month it belongs to, and the exact UTC instant it was posted. Predictions are
probability distributions over hourly slots, built from two parts:

  day     which day of the month the drop lands on: a kernel over past drops,
          weighted by recency and boosted for the same calendar month in earlier
          years (December and August behave differently from the rest).
  hour    what time of day: a circular kernel over recent drop times, all types
          pooled, since one team sends them all.

Pre- and post-monthly bonuses do not happen every month, so they also carry a
chance of happening at all, which falls as the usual window passes without one.
"""
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import traffic

DATA = Path(__file__).parent / "data"
SITES = {"stake.com": "StakeCasino", "stake.us": "StakeSocial"}
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]

DAY_START = timedelta(hours=4)    # a "drop day" runs 04:00-04:00 UTC so late-night drops stay on their day
DAY_HALF_LIFE = 270               # days; older drops count half as much per this many days
HOUR_HALF_LIFE = 90               # time-of-day habits shift faster (late-night UTC -> morning UTC in 2026)
SAME_MONTH_BOOST = 10.0           # weight for the same calendar month in earlier years (tuned by backtest)
DAY_BANDWIDTH = {"monthly": 2.5, "pre": 1.5, "post": 4.0}  # days, tuned by backtest log-likelihood
HOUR_BANDWIDTH = 1.2              # hours
OFFSETS = range(-4, 35)           # day offsets from the 1st of the bonus's month that a drop may land on
POST_MIN_GAP = 5                  # a post-monthly never lands within this many days of the monthly

UTC = timezone.utc


# ---------------------------------------------------------------- events

def classify(text):
    title = text.split("\n", 1)[0].lower()
    head = text[:300].lower()
    if "clash" in title or "issue" in title or "giveaway" in title:
        return None
    if re.search(r"pre[\s-]*monthly", head):
        return "pre"
    if re.search(r"post[\s-]*monthly", head):
        return "post"
    if "monthly" not in head:
        return None
    if re.search(r"#2|second|2nd|another|special monthly", head):
        return "extra"
    return "monthly"


def month_start(year, month):
    return datetime(year, month, 1, tzinfo=UTC)


def add_months(ym, n):
    y, m = ym
    m += n
    return (y + (m - 1) // 12, (m - 1) % 12 + 1)


def target_month(text, when):
    """The month a bonus belongs to: named in the title, else the posting month."""
    head = text[:200].lower()
    named = next((i + 1 for i, name in enumerate(MONTHS) if name in head), None)
    if named is None:
        return (when.year, when.month)
    candidates = [(when.year + dy, named) for dy in (-1, 0, 1)]
    return min(candidates, key=lambda ym: abs((month_start(*ym) - when).total_seconds()))


def day_offset(when, ym):
    return math.floor((when - month_start(*ym) - DAY_START) / timedelta(days=1))


def load_events(site):
    path = DATA / f"posts_{SITES[site]}.json"
    if not path.exists():
        return []
    posts = json.loads(path.read_text("utf-8"))["posts"]
    events = []
    for p in posts:
        kind = classify(p["text"])
        if not kind:
            continue
        when = datetime.fromisoformat(p["time"]).astimezone(UTC)
        ym = target_month(p["text"], when)
        # the same drop announced twice within a day
        if any(e["kind"] == kind and abs((e["time"] - when).total_seconds()) < 86400 for e in events):
            continue
        events.append({"kind": kind, "time": when, "month": ym, "title": p["text"].split("\n", 1)[0].strip(),
                       "post": f"https://t.me/{SITES[site]}/{p['id']}"})
    events.sort(key=lambda e: e["time"])
    # a second plain "Monthly" for the same month is an extra drop, not the monthly
    seen = set()
    for e in events:
        if e["kind"] == "monthly":
            if e["month"] in seen:
                e["kind"] = "extra"
            seen.add(e["month"])
    for e in events:
        e["offset"] = day_offset(e["time"], e["month"])
    return events


# ---------------------------------------------------------------- model parts

def weight(e, now, ym, half_life):
    age = max((now - e["time"]).days, 0)
    w = 0.5 ** (age / half_life)
    if e["month"][1] == ym[1]:
        w *= SAME_MONTH_BOOST
    return w


def day_distribution(events, now, ym, bandwidth):
    """P(drop lands on day offset k of month ym), from past drops of one type."""
    probs = {k: 1e-6 for k in OFFSETS}
    for e in events:
        w = weight(e, now, ym, DAY_HALF_LIFE)
        for k in OFFSETS:
            probs[k] += w * math.exp(-0.5 * ((k - e["offset"]) / bandwidth) ** 2)
    total = sum(probs.values())
    return {k: v / total for k, v in probs.items()}


def hour_distribution(events, now):
    """P(drop at UTC hour h), circular over the day, all bonus types pooled."""
    probs = [1e-4] * 24
    for e in events:
        w = 0.5 ** (max((now - e["time"]).days, 0) / HOUR_HALF_LIFE)
        h0 = e["time"].hour + e["time"].minute / 60
        for h in range(24):
            d = min(abs(h + 0.5 - h0), 24 - abs(h + 0.5 - h0))
            probs[h] += w * math.exp(-0.5 * (d / HOUR_BANDWIDTH) ** 2)
    total = sum(probs)
    return [p / total for p in probs]


def slots_for(days, hours, ym, min_time=None):
    """Hourly slots (UTC instant, probability) for a month's day distribution."""
    out = []
    base = month_start(*ym) + DAY_START
    for k, pd in days.items():
        for i in range(24):
            hour = (4 + i) % 24
            t = base + timedelta(days=k, hours=i)
            if min_time and t < min_time:
                continue
            out.append((t, pd * hours[hour]))
    total = sum(p for _, p in out) or 1
    return [(t, p / total) for t, p in out]


def occurrence_prior(site_events, kind, now, ym):
    """Chance a pre/post-monthly happens in month ym, from months the channel covered."""
    covered = sorted({e["month"] for e in site_events if e["kind"] == "monthly" and e["time"] < now})
    hits = {e["month"] for e in site_events if e["kind"] == kind}
    num = den = 0.0
    for m in covered:
        if m >= ym:
            continue
        age = max((now - month_start(*m)).days, 0)
        w = 0.5 ** (age / DAY_HALF_LIFE) * (SAME_MONTH_BOOST if m[1] == ym[1] else 1)
        den += w
        num += w * (m in hits)
    return (num + 0.25) / (den + 1.0)  # light prior so a type never reads as certain or impossible


# ---------------------------------------------------------------- predictions

def predict_kind(site_events, kind, now, ym, after=None):
    """Distribution for one bonus type in month ym, before conditioning on `now`."""
    past = [e for e in site_events if e["kind"] == kind and e["time"] < now]
    days = day_distribution(past, now, ym, DAY_BANDWIDTH[kind])
    hours = hour_distribution([e for e in site_events if e["time"] < now], now)
    slots = slots_for(days, hours, ym, min_time=after)
    prior = 1.0 if kind == "monthly" else occurrence_prior(site_events, kind, now, ym)
    return {"month": ym, "prior": prior, "slots": slots, "basis": len(past)}


def remaining(slots, now):
    return sum(p for t, p in slots if t >= now)


def summarize(pred, now):
    """Conditioned on nothing having dropped by `now`: chance it still happens, peak, windows."""
    left = remaining(pred["slots"], now)
    prior = pred["prior"]
    chance = prior * left / (prior * left + (1 - prior)) if prior < 1 else 1.0
    future = [(t, p / left) for t, p in pred["slots"] if t >= now] if left > 0 else []
    if not future:
        return {"chance": 0.0}
    peak_day = {}
    for t, p in future:
        d = (t - DAY_START).date()
        peak_day[d] = peak_day.get(d, 0) + p
    best = max(future, key=lambda tp: tp[1])[0]

    def window(mass):
        cum, lo, hi = 0.0, None, None
        for t, p in future:
            cum += p
            if lo is None and cum >= (1 - mass) / 2:
                lo = t
            if hi is None and cum >= 1 - (1 - mass) / 2:
                hi = t
        return lo, hi or future[-1][0]

    def within(h):
        return chance * sum(p for t, p in future if t < now + timedelta(hours=h))

    return {"chance": chance, "peak": best, "peak_day": max(peak_day, key=peak_day.get),
            "w50": window(0.5), "w80": window(0.8), "p24": within(24), "p7d": within(168),
            "left": left}


def next_target(site_events, kind, now):
    """The month whose `kind` drop is still to come, plus the reason when a month was skipped."""
    ym = (now.year, now.month)
    done = {e["month"] for e in site_events if e["kind"] == kind}
    monthly_done = {e["month"]: e for e in site_events if e["kind"] == "monthly"}
    # December's monthly lands on Nov 30, so look at last month too
    for cand in (add_months(ym, -1), ym, add_months(ym, 1), add_months(ym, 2)):
        if cand in done:
            continue
        if kind == "pre" and cand in monthly_done:
            continue  # a pre-monthly only comes before that month's monthly
        after = None
        if kind == "post":
            m = monthly_done.get(cand)
            after = m["time"] + timedelta(days=POST_MIN_GAP) if m else None
        pred = predict_kind(site_events, kind, now, cand, after)
        if remaining(pred["slots"], now) < 0.02:
            continue  # its window has passed
        return pred
    return None


def backtest(site_events, kind, months=18):
    """Predict each past drop using only what was known 4 days before its month started."""
    rows = []
    targets = [e for e in site_events if e["kind"] == kind][-months:]
    for e in targets:
        cutoff = month_start(*e["month"]) - timedelta(days=4)
        prior_events = [x for x in site_events if x["time"] < cutoff]
        if len([x for x in prior_events if x["kind"] == kind]) < 4:
            continue
        pred = predict_kind(prior_events, kind, cutoff, e["month"])
        s = summarize(pred, cutoff)
        actual = (e["time"] - DAY_START).date()
        lo, hi = ((s["w80"][0] - DAY_START).date(), (s["w80"][1] - DAY_START).date())
        rows.append({"month": f"{e['month'][0]}-{e['month'][1]:02d}", "actual": e["time"].isoformat(),
                     "predicted": s["peak_day"].isoformat(), "lo": lo.isoformat(), "hi": hi.isoformat(),
                     "err": abs((actual - s["peak_day"]).days), "hit": lo <= actual <= hi,
                     "naive_err": abs(actual.day - 15) if actual.month == e["month"][1] else None})
    return rows


def next_weekly(now):
    """Weekly bonuses go out to VIPs every Saturday 12:30 GMT (Stake help centre)."""
    days = (5 - now.weekday()) % 7
    t = (now + timedelta(days=days)).replace(hour=12, minute=30, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=7)
    return t


def build(now=None):
    now = now or datetime.now(UTC)
    out = {"generated": now.isoformat(), "sites": {}}
    for site in SITES:
        events = load_events(site)
        site_out = {"bonuses": {}, "history": [], "backtest": {}}
        preds = {}
        for kind in ("monthly", "pre", "post"):
            pred = next_target(events, kind, now)
            if not pred:
                continue
            preds[kind] = dict(pred, summary=summarize(pred, now))
            ym = pred["month"]
            this = [e for e in events if e["month"] == ym]
            site_out["bonuses"][kind] = {
                "month": f"{ym[0]}-{ym[1]:02d}", "prior": pred["prior"], "basis": pred["basis"],
                "slots": [[int(t.timestamp()), round(p, 7)] for t, p in pred["slots"] if p > 1e-6],
                "monthly_at": next((e["time"].isoformat() for e in this if e["kind"] == "monthly"), None),
            }
            site_out["backtest"][kind] = backtest(events, kind)
        last = {}
        for e in events:
            last[e["kind"]] = e
        site_out["latest"] = {k: {"time": e["time"].isoformat(), "title": e["title"], "post": e["post"],
                                  "month": f"{e['month'][0]}-{e['month'][1]:02d}"} for k, e in last.items()}
        site_out["history"] = [{"kind": e["kind"], "time": e["time"].isoformat(), "title": e["title"],
                                "month": f"{e['month'][0]}-{e['month'][1]:02d}", "offset": e["offset"],
                                "post": e["post"]} for e in events]
        site_out["hours"] = hour_distribution([e for e in events if e["kind"] == "monthly"], now)
        site_out["weekly"] = next_weekly(now).isoformat()
        site_out["traffic"] = traffic.analyse(site, events, preds, now)
        out["sites"][site] = site_out
    return out
