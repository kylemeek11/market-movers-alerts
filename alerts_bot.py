#!/usr/bin/env python3
"""
Market Movers phone alerts.

Runs on GitHub Actions, independent of any Mac. Screens the whole US market
for stocks up past a set of thresholds and pushes a notification to your
phone via ntfy when one crosses a level it hasn't crossed today.

Deliberately light: it only needs the screener's quote payload, not price
history, so a run is a single API call and finishes in seconds.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Settings (match market_movers.py) --------------------------------------
MIN_GAIN_PCT = 5.0
MIN_DOLLAR_VOLUME = 25_000_000
MIN_MARKET_CAP = 100_000_000
MIN_PRICE = 3.00
MIN_DAY_VOLUME = 100_000

ALERT_LEVELS = [10.0, 15.0, 20.0, 30.0, 50.0]
MAX_ALERTS_PER_RUN = 6

MARKET_TZ = "America/New_York"
MARKET_OPEN_HOUR = 8
MARKET_CLOSE_HOUR = 17

STATE_FILE = Path(os.environ.get("STATE_FILE", "state/alerts.json"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------

def market_is_open():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(timezone.utc).astimezone(ZoneInfo(MARKET_TZ))
    except Exception:
        return True          # if the timezone lookup fails, don't go silent
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN_HOUR <= now.hour < MARKET_CLOSE_HOUR


def push(title, message, priority="default", tags="chart_with_upwards_trend"):
    """Send one notification. Headers must be ASCII; the body may be UTF-8."""
    import urllib.request

    if not NTFY_TOPIC:
        log("NTFY_TOPIC is not set — printing instead of sending")
        log(f"  {title} | {message}")
        return

    def ascii_only(s):
        return "".join(ch if 32 <= ord(ch) < 127 else "-" for ch in str(s))

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=str(message).encode("utf-8"),
        headers={
            "Title": ascii_only(title)[:200],
            "Priority": priority,
            "Tags": tags,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 300:
                log(f"  ntfy returned {resp.status}")
    except Exception as exc:
        log(f"  push failed: {exc}")


def screen(yf):
    from yfinance import EquityQuery

    def build(with_cap):
        terms = [
            EquityQuery("gt", ["percentchange", MIN_GAIN_PCT]),
            EquityQuery("eq", ["region", "us"]),
            EquityQuery("gt", ["dayvolume", MIN_DAY_VOLUME]),
            EquityQuery("gt", ["intradayprice", MIN_PRICE]),
        ]
        if with_cap:
            terms.append(EquityQuery("gt", ["intradaymarketcap", MIN_MARKET_CAP]))
        return EquityQuery("and", terms)

    quotes, with_cap, offset = [], True, 0
    while True:
        try:
            resp = yf.screen(build(with_cap), offset=offset, size=250,
                             sortField="percentchange", sortAsc=False)
        except Exception as exc:
            if with_cap:
                log(f"  market-cap filter rejected ({exc}); filtering locally")
                with_cap = False
                continue
            log(f"  screener failed: {exc}")
            break
        batch = (resp or {}).get("quotes", []) or []
        quotes.extend(batch)
        if len(batch) < 250 or len(quotes) >= 250:
            break
        offset += 250

    out = []
    for q in quotes:
        if not q.get("symbol"):
            continue
        chg = q.get("regularMarketChangePercent")
        price = q.get("regularMarketPrice") or 0
        vol = q.get("regularMarketVolume") or 0
        if chg is None or chg < MIN_GAIN_PCT:
            continue
        if price < MIN_PRICE or vol < MIN_DAY_VOLUME:
            continue
        if (q.get("marketCap") or 0) < MIN_MARKET_CAP:
            continue
        if price * vol < MIN_DOLLAR_VOLUME:
            continue
        out.append({
            "symbol": q["symbol"],
            "name": q.get("shortName") or q.get("longName") or q["symbol"],
            "pct": float(chg),
            "price": float(price),
            "dollars": float(price) * float(vol),
        })
    out.sort(key=lambda r: -r["pct"])
    return out


def load_state():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        state = json.loads(STATE_FILE.read_text())
        if state.get("date") == today:
            return state
        log("  state is from a previous day — starting fresh")
    except (OSError, ValueError):
        log("  no previous state found")
    return {"date": today, "fired": {}}


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except OSError as exc:
        log(f"  could not save state: {exc}")


def main():
    if os.environ.get("FORCE_RUN") != "1" and not market_is_open():
        log("Outside market hours — nothing to do")
        return 0

    if os.environ.get("TEST_PUSH") == "1":
        push("Market Movers test", "Phone alerts are wired up correctly.",
             priority="default", tags="white_check_mark")
        log("Test notification sent")
        return 0

    try:
        import yfinance as yf
    except ImportError:
        log("yfinance is not installed")
        return 2

    rows = screen(yf)
    log(f"{len(rows)} stocks pass the filters")

    state = load_state()
    fired = state["fired"]

    pending = []
    for r in rows:
        crossed = [lv for lv in ALERT_LEVELS if r["pct"] >= lv]
        if not crossed:
            continue
        highest = max(crossed)
        if highest <= fired.get(r["symbol"], 0.0):
            continue
        pending.append((highest, r))

    pending.sort(key=lambda t: -t[1]["pct"])
    log(f"{len(pending)} new threshold crossings")

    for level, r in pending[:MAX_ALERTS_PER_RUN]:
        fired[r["symbol"]] = level
        body = (f"{r['name']}\n"
                f"now +{r['pct']:.1f}%  ·  ${r['price']:,.2f}  ·  "
                f"${r['dollars'] / 1e6:,.0f}M traded")
        push(f"{r['symbol']} crossed +{level:.0f}%", body,
             priority="high" if level >= 20 else "default")
        log(f"  ALERT {r['symbol']} +{r['pct']:.1f}% (crossed {level:.0f}%)")

    if len(pending) > MAX_ALERTS_PER_RUN:
        extra = len(pending) - MAX_ALERTS_PER_RUN
        push("More movers crossing",
             f"{extra} more crossed a level. They'll follow shortly.",
             tags="fire")
        log(f"  {extra} queued for the next run")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
