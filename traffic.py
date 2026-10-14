"""When are the most players online? Hourly player counts on Stake Engine games.

Source: engineplayers.com's public peak-hours feed, which samples live player
counts on every engine.io game (Stake Engine slots on Stake.com and Stake.us)
every few minutes. Each month comes back as a date x UTC-hour grid of average
players. Finished months never change, so they are cached; only the current and
previous month are fetched again.

There is no public turnover history (only all-time totals per game), so player
counts stand in for activity; on Stake Engine games the two move together.
"""
import json
import math
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).parent / "data" / "traffic"
API = "https://engineplayers.com/api/peak-hours?site={site}&month={month}"
SITES = {"all": "all", "stake.com": "stake.com", "stake.us": "stake.us"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
UTC = timezone.utc


def fetch_month(site, month):
    req = urllib.request.Request(API.format(site=site, month=month), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def update(log=print):
    """Fetch what is missing or still changing; returns the list of months stored."""
    DATA.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).date()
    this_month = f"{today:%Y-%m}"
    prev_month = f"{(today.replace(day=1) - timedelta(days=1)):%Y-%m}"
    index_path = DATA / "months.json"
    months = json.loads(index_path.read_text("utf-8")) if index_path.exists() else []
    fetched = 0
    try:
        if not months or this_month not in months:
            months = fetch_month("all", this_month).get("months", months) or months
            index_path.write_text(json.dumps(months), "utf-8")
        for site in SITES.values():
            for month in months:
                path = DATA / f"{site}_{month}.json"
                if path.exists() and month not in (this_month, prev_month):
                    continue
                if path.exists() and month == prev_month and path.stat().st_mtime > time.time() - 86400 * 3:
                    continue
                if path.exists() and month == this_month and path.stat().st_mtime > time.time() - 1800:
                    continue
                d = fetch_month(site, month)
                path.write_text(json.dumps({k: d[k] for k in ("dateStrings", "grid", "maxGrid")}), "utf-8")
                fetched += 1
                time.sleep(0.3)
        log(f"traffic: {fetched} month file(s) refreshed, {len(months)} months on record")
    except Exception as e:  # offline or the tracker is down: keep what is cached
        log(f"traffic: fetch failed ({e}); using cached data")
    return months


def load_hours(site):
    """{utc datetime of the hour: average players}, skipping hours with no samples."""
    hours = {}
    for path in sorted(DATA.glob(f"{site}_*.json")):
        d = json.loads(path.read_text("utf-8"))
        for ds, row in zip(d["dateStrings"], d["grid"]):
            day = date.fromisoformat(ds)
            for h, v in enumerate(row):
                if v > 0:
                    hours[datetime(day.year, day.month, day.day, h, tzinfo=UTC)] = v
    return dict(sorted(hours.items()))


def bonus_hours(events):
    """Hours within a day after any bonus drop: kept out of the 'normal week' baseline."""
    out = set()
    for e in events:
        t0 = e["time"].replace(minute=0, second=0, microsecond=0)
        out.update(t0 + timedelta(hours=h) for h in range(24))
    return out


def daily_means(hours, skip=()):
    daily = {}
    for t, v in hours.items():
        if t not in skip:
            daily.setdefault(t.date(), []).append(v)
    return {d: sum(v) / len(v) for d, v in daily.items() if len(v) >= 18}


def bonus_effect(hours, events):
    """Players in the 24 hours after a drop vs the same 24 hours a week before and after."""
    out = {}
    for kind in ("monthly", "post", "pre"):
        ratios = []
        for e in events:
            if e["kind"] != kind:
                continue
            t0 = e["time"].replace(minute=0, second=0, microsecond=0)
            after = [hours.get(t0 + timedelta(hours=h)) for h in range(24)]
            ref = [[hours.get(t0 + timedelta(days=w, hours=h)) for h in range(24)] for w in (-7, 7)]
            refs = [sum(r) / 24 for r in ref if None not in r]
            if None in after or not refs:
                continue
            ratios.append(sum(after) / 24 / (sum(refs) / len(refs)))
        if ratios:
            ratios.sort()
            med = ratios[len(ratios) // 2] if len(ratios) % 2 else sum(ratios[len(ratios) // 2 - 1:len(ratios) // 2 + 1]) / 2
            # shrink small samples toward "no effect"
            out[kind] = {"n": len(ratios), "median": round(med, 3),
                         "uplift": round((med - 1) * len(ratios) / (len(ratios) + 2), 3)}
    return out


def drop_chance_by_hour(preds, now):
    """{hour: P(a bonus of this kind dropped in the 24 hours before it)} from the radar's slots."""
    out = {}
    for kind, pred in preds.items():
        s = pred["summary"]
        if not s.get("chance"):
            continue
        future = [(t, p * s["chance"] / s["left"]) for t, p in pred["slots"] if t >= now]
        start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        cur = {}
        for i in range(24 * 14):
            h = start + timedelta(hours=i)
            cur[h] = sum(p for t, p in future if h - timedelta(hours=24) < t <= h)
        out[kind] = cur
    return out


def analyse(site, events, preds=None, now=None):
    now = now or datetime.now(UTC)
    hours = load_hours(site)
    if not hours:
        return None
    skip = bonus_hours([e for e in events if e["time"] < now])
    day_mean = daily_means(hours, skip)
    days_sorted = sorted(day_mean)

    def level(d):
        win = [day_mean[x] for x in days_sorted if abs((x - d).days) <= 3]
        return sum(win) / len(win) if len(win) >= 5 else None

    all_days = daily_means(hours)
    levels = {d: level(d) for d in sorted(all_days)}
    # hour-of-week index (UTC): an hour's players relative to that week's level, bonus days left out
    how = [[[] for _ in range(24)] for _ in range(7)]
    for t, v in hours.items():
        lv = levels.get(t.date())
        if lv and t not in skip:
            how[t.weekday()][t.hour].append(v / lv)
    how_idx = [[(sum(c) / len(c) if c else None) for c in row] for row in how]
    dow = [[] for _ in range(7)]
    for d, m in day_mean.items():
        if levels.get(d):
            dow[d.weekday()].append(m / levels[d])
    dow_idx = [sum(x) / len(x) if x else None for x in dow]
    # every day (bonus days included) against its surrounding normal week
    lifts = sorted(((m / levels[d], d) for d, m in all_days.items() if levels.get(d)), reverse=True)
    kinds = {}
    for e in events:
        kinds.setdefault((e["time"] - timedelta(hours=0)).date(), []).append(e["kind"])
    effect = bonus_effect(hours, events)
    by_month = {}
    for d, m in all_days.items():
        by_month.setdefault(f"{d:%Y-%m}", []).append(m)
    # forecast: recent normal level x hour-of-week shape x bonus uplift where a drop is likely
    recent = [day_mean[d] for d in days_sorted if d < now.date()][-14:]
    base = sum(recent) / len(recent) if recent else None
    chance = drop_chance_by_hour(preds or {}, now)
    forecast = []
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for i in range(24 * 14):
        t = start + timedelta(hours=i)
        idx = how_idx[t.weekday()][t.hour]
        if idx is None or base is None:
            continue
        lift = 1 + sum(effect[k]["uplift"] * chance[k].get(t, 0) for k in chance if k in effect)
        forecast.append([int(t.timestamp()), round(base * idx * lift), round(lift, 3)])
    record = max(hours.items(), key=lambda kv: kv[1])
    return {
        "site": site, "first": min(hours).isoformat(), "last": max(hours).isoformat(), "days": len(all_days),
        "how": how_idx, "dow": dow_idx, "effect": effect, "base": round(base) if base else None,
        "top_days": [{"date": d.isoformat(), "lift": round(l, 3), "players": round(all_days[d]),
                      "bonus": kinds.get(d, []) + [k + " (day before)" for k in kinds.get(d - timedelta(days=1), [])]}
                     for l, d in lifts[:10]],
        "months": {k: round(sum(v) / len(v)) for k, v in sorted(by_month.items())},
        "daily": [[d.isoformat(), round(m)] for d, m in sorted(all_days.items())],
        "forecast": forecast,
        "record": {"time": record[0].isoformat(), "players": record[1]},
    }


if __name__ == "__main__":
    import model
    update()
    for site in ("stake.com", "stake.us"):
        a = analyse(site, model.load_events(site))
        print(f"== {site}: {a['first'][:10]} .. {a['last'][:10]}, {a['days']} days, record {a['record']}")
        print("   bonus effect:", a["effect"])
        print("   day-of-week:", " ".join(f"{DAYS[i]} {v:.3f}" for i, v in enumerate(a["dow"]) if v))
        best = sorted(((v, d, h) for d, row in enumerate(a["how"]) for h, v in enumerate(row) if v), reverse=True)
        print("   best UTC hours:", ", ".join(f"{DAYS[d]} {h:02d}:00 {v:.2f}" for v, d, h in best[:5]))
        for x in a["top_days"]:
            print("   ", x)
