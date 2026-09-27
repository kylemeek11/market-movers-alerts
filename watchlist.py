"""
Watchlist: coins Kyle wants watched more closely than the general screen.

Two extra signals, on top of everything alerts_bot.py already does:

1. EARLY CLIMB - the ordinary rate signal (move over 50-75 minutes on
   relative volume) at a lower bar, WATCH_RATE_PCT instead of
   CRYPTO_RATE_PCT. It shares the ordinary signal's cooldown key, so a coin
   never buzzes twice for the same move.

2. DIP TURNING UP - the coin is at least `dip_pct` below its 7-day high AND
   has bounced `turn_pct` off its 12-hour low. The bounce requirement is there
   so the alert is not an invitation to catch a falling knife.

What the dip rule is worth, measured before it shipped (2026-09-27, Coinbase
hourly candles, 100 days, ETH LINK ONDO LDO XRP SOL BTC, 24h cooldown):

    rule                     signals  72h return  net of 1.9% costs > 0
    any hour at random          -        +1.9%          38%
    dip 8%, turn 2% (default)  117       +2.2%          44%
    dip 12%, turn 2%            47       +1.1%          28%
    dip 15%, turn 2%            18       -1.3%          33%

A small edge at 8%, and deeper dips were WORSE, not better, over a period
when the whole market was rising. Treat the alert as a well-timed entry
point, not as a prediction.

Data: one Coinbase hourly-candle request per watched coin per run. The
config lives in watchlist.json so it can be edited from the phone.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

WATCHLIST_FILE = Path("watchlist.json")
API = "https://api.exchange.coinbase.com"

WATCH_RATE_PCT = 2.0           # early-climb bar for watched coins (general: 3.0)
DIP_PCT = 8.0                  # default: this far below the 7-day high
TURN_PCT = 2.0                 # default: and this far up off the 12-hour low
HIGH_WINDOW_H = 168            # 7 days
LOW_WINDOW_H = 12
DIP_COOLDOWN_SEC = 24 * 3600   # one dip alert per coin per day


def load_watchlist(path=WATCHLIST_FILE):
    """The watched coins, or [] if the file is missing or unreadable."""
    try:
        cfg = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    out = []
    for c in cfg.get("coins") or []:
        sym = str(c.get("symbol") or "").upper().strip()
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "product": c.get("product") or f"{sym}-USD",
            "dip_pct": float(c.get("dip_pct") or DIP_PCT),
            "turn_pct": float(c.get("turn_pct") or TURN_PCT),
            "why": c.get("why") or "",
        })
    return out


def fetch_hourly(product, hours=HIGH_WINDOW_H + 2):
    """Hourly candles, oldest first: [time, low, high, open, close, volume]."""
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=min(hours, 299))
    url = (f"{API}/products/{product}/candles?granularity=3600"
           f"&start={start:%Y-%m-%dT%H:%M:%SZ}&end={end:%Y-%m-%dT%H:%M:%SZ}")
    req = urllib.request.Request(url, headers={"User-Agent": "watchlist/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    return sorted(rows, key=lambda c: int(c[0]))


def dip_state(candles, price, dip_pct, turn_pct):
    """Where the coin sits against its 7-day high and 12-hour low.

    `price` is the live price; candles supply the high and low. Returns None
    when there is not enough history to judge.
    """
    if not candles or not price or len(candles) < LOW_WINDOW_H:
        return None
    week = candles[-HIGH_WINDOW_H:]
    recent = candles[-LOW_WINDOW_H:]
    high = max(max(float(c[2]) for c in week), price)
    low = min(min(float(c[1]) for c in recent), price)
    off_high = (1 - price / high) * 100.0
    off_low = (price / low - 1) * 100.0
    in_zone = off_high >= dip_pct
    return {
        "high": high, "low": low,
        "off_high": off_high, "off_low": off_low,
        "in_zone": in_zone,
        "turning": in_zone and off_low >= turn_pct,
    }


def watch_rows(crypto_rows, coins):
    """The screener rows for watched coins (already Robinhood-filtered)."""
    wanted = {c["symbol"] for c in coins}
    return [r for r in crypto_rows if r["symbol"] in wanted]


def check_dips(coins, prices, fired, now_ts, overnight, send, fetch=None,
               log=print):
    """Send a dip-turning alert for each watched coin that qualifies.

    `prices` maps symbol -> live price (from the screener). `send(coin, st,
    price)` delivers one alert. Overnight signals are held WITHOUT being
    recorded, the same as every other alert, so a dip still turning at
    breakfast buzzes then. Returns the number sent.
    """
    fetch = fetch or fetch_hourly
    sent = held = 0
    for c in coins:
        price = prices.get(c["symbol"])
        if not price:
            continue
        key = f"dip:crypto:{c['symbol']}"
        if now_ts - fired.get(key, 0) < DIP_COOLDOWN_SEC:
            continue
        try:
            candles = fetch(c["product"])
        except Exception as exc:
            log(f"  watchlist: {c['symbol']} candles failed ({exc})")
            continue
        st = dip_state(candles, price, c["dip_pct"], c["turn_pct"])
        if st is None:
            continue
        log(f"  watch {c['symbol']}: {st['off_high']:.1f}% below 7d high, "
            f"+{st['off_low']:.1f}% off 12h low"
            + (" - DIP TURNING" if st["turning"] else
               " - in dip zone" if st["in_zone"] else ""))
        if not st["turning"]:
            continue
        if overnight:
            held += 1
            continue
        fired[key] = now_ts
        send(c, st, price)
        sent += 1
    if held:
        log(f"  {held} watchlist dip signals held until morning")
    return sent
