#!/usr/bin/env python3
"""Stop-order recalculator.

Runs on a schedule, re-prices Kyle's crypto stops from live data, and pushes a
notification ONLY when a stop actually needs changing.

It never places, cancels, or modifies an order. It has no Robinhood
credentials and no ability to trade. It reads public market data, does
arithmetic, and sends a message. Entering orders stays manual, on purpose.

The method it implements is written up in the MarketMovers project doc
"Recalculating crypto stop orders". The short version:

  stop = price x (1 - room) x (1 - spread)

where `room` is how far the real market should be allowed to fall and the
spread correction exists because Robinhood evaluates a stop against its own
marked-down bid, so an uncorrected stop fires about a spread early.

`room` is measured, not guessed: 1.5x the coin's 95th-percentile overnight
drawdown over the recent sample, floored and capped. If the coin is in a
violent intraday move, the width widens instead of tightening - a stop is live
24 hours a day, and on a running coin the daytime swings dwarf the overnight
ones.
"""

import json
import os
import statistics
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Tunables ---------------------------------------------------------------

CONFIG_FILE = Path(os.environ.get("POSITIONS_FILE", "positions.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/stops.json"))

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

API = "https://api.exchange.coinbase.com"

# Overnight window, UTC. 03:00-12:00 UTC is 22:00-07:00 US Central.
NIGHT_START_UTC = 3
NIGHT_HOURS = 9

# Width bounds. The floor stops a very quiet coin getting a hair-trigger stop;
# the cap stops a wild one getting a width so wide the stop is decorative.
MIN_ROOM_PCT = 5.0
MAX_ROOM_PCT = 12.0
P95_MULTIPLE = 1.5

# A coin whose last 6 hours have ranged more than this multiple of its own
# typical 6-hour range is "running" - mid-move, not a normal session.
RUNNING_MULTIPLE = 2.5
RUNNING_MIN_ROOM_PCT = 10.0

# How far the live stop has to drift from the recommendation before it is
# worth a notification, as a percentage of the current price.
DRIFT_PCT = 2.0
# Quiet hours are OFF by Kyle's choice (2026-09-24): every run, including the
# 3am one, uses the same DRIFT_PCT bar. Set QUIET_HOURS_ENABLED back to True
# to restore a softer overnight window - the hours below are kept ready for
# that, and OVERNIGHT_DRIFT_PCT is inert until it happens.
QUIET_HOURS_ENABLED = False
OVERNIGHT_DRIFT_PCT = 4.0
USER_TZ = "America/Chicago"
OVERNIGHT_START_HOUR = 0
OVERNIGHT_END_HOUR = 8

# Don't re-send the same advice. Suppress a repeat within this many hours
# unless the recommendation itself has moved by MOVED_PCT of price.
SUPPRESS_HOURS = 6
MOVED_PCT = 1.0

# A stop this close to the market is about to fire by accident.
TOO_CLOSE_PCT = 1.5

MIN_NIGHTS = 5


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def local_now(tz_name=USER_TZ):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    except Exception:
        return None


def is_overnight(hour=None):
    """True inside the quiet window, which may or may not wrap past midnight.

    The modulo keeps both cases in one expression: with a window of 0-8 it is
    a plain range, and with one of 22-7 it wraps. Passing `hour` is for tests.
    """
    if not QUIET_HOURS_ENABLED:
        return False
    if hour is None:
        now = local_now()
        if now is None:
            return False
        hour = now.hour
    span = (OVERNIGHT_END_HOUR - OVERNIGHT_START_HOUR) % 24
    return (hour - OVERNIGHT_START_HOUR) % 24 < span


# --- Market data ------------------------------------------------------------

def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "stop-check/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_price(product):
    """Live mid price. Falls back to the newest candle if the ticker fails."""
    try:
        t = fetch_json(f"{API}/products/{product}/ticker")
        price = float(t.get("price") or 0)
        if price > 0:
            return price
    except Exception as exc:
        log(f"  {product}: ticker failed ({exc})")
    return None


CANDLE_CHUNKS = 4          # 300 hourly candles each -> ~50 nights of history


def fetch_candles(product, chunks=CANDLE_CHUNKS):
    """Hourly candles, newest first: [time, low, high, open, close, vol].

    Coinbase caps a request at 300 candles, which is only 12.5 days. The
    95th-percentile night is a tail statistic and a fortnight of calm weather
    understates it, so page back a few windows and stitch them together.
    """
    import time as _time

    by_time = {}
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for _ in range(max(1, chunks)):
        start = end - timedelta(hours=299)
        url = (f"{API}/products/{product}/candles?granularity=3600"
               f"&start={start:%Y-%m-%dT%H:%M:%SZ}"
               f"&end={end:%Y-%m-%dT%H:%M:%SZ}")
        try:
            batch = fetch_json(url)
        except Exception as exc:
            log(f"  {product}: history chunk failed ({exc})")
            break
        if not batch:
            break
        for c in batch:
            by_time[int(c[0])] = c
        end = start - timedelta(hours=1)
        _time.sleep(0.35)          # public endpoint; don't hammer it
    return sorted(by_time.values(), key=lambda c: -int(c[0]))


# --- Measurement ------------------------------------------------------------

def overnight_drawdowns(candles, nights=60):
    """Percent drop from the 03:00 UTC open to the lowest low through 12:00.

    Returns a list of (date string, drawdown percent), newest first. Nights
    with too many missing hours are skipped rather than guessed at.
    """
    by_time = {int(c[0]): c for c in candles}
    if not by_time:
        return []
    newest = datetime.fromtimestamp(max(by_time), tz=timezone.utc)
    out = []
    for back in range(1, nights + 1):
        day = (newest - timedelta(days=back)).replace(
            hour=NIGHT_START_UTC, minute=0, second=0, microsecond=0)
        start = int(day.timestamp())
        first = by_time.get(start)
        if not first:
            continue
        ref = float(first[3])          # open at 03:00
        low, have = None, 0
        for h in range(NIGHT_HOURS):
            c = by_time.get(start + h * 3600)
            if c:
                have += 1
                lo = float(c[1])
                low = lo if low is None else min(low, lo)
        if have < NIGHT_HOURS - 2 or not ref or low is None:
            continue
        out.append((day.strftime("%Y-%m-%d"), (ref - low) / ref * 100.0))
    return out


def percentile(values, q):
    """Nearest-rank percentile. Small samples make fancier methods a lie."""
    if not values:
        return None
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return vals[idx]


def intraday_regime(candles, window=6):
    """Is this coin mid-move right now?

    Compares the range of the last `window` hours against the median range of
    every `window`-hour block in the sample.
    """
    if len(candles) < window * 3:
        return False, None, None
    ordered = sorted(candles, key=lambda c: int(c[0]))   # oldest first

    def block_range(block):
        hi = max(float(c[2]) for c in block)
        lo = min(float(c[1]) for c in block)
        base = float(block[0][3]) or lo
        if not base:
            return None
        return (hi - lo) / base * 100.0

    ranges = []
    for i in range(0, len(ordered) - window):
        r = block_range(ordered[i:i + window])
        if r is not None:
            ranges.append(r)
    current = block_range(ordered[-window:])
    if current is None or not ranges:
        return False, None, None
    typical = statistics.median(ranges)
    running = typical > 0 and current > typical * RUNNING_MULTIPLE
    return running, current, typical


def recommend_room(p95, running, override=None):
    """How much room to give the market, in percent."""
    if override is not None:
        return float(override), "override"
    if p95 is None:
        return MIN_ROOM_PCT, "no data - floor"
    room = max(MIN_ROOM_PCT, min(MAX_ROOM_PCT, P95_MULTIPLE * p95))
    reason = f"1.5x p95 night ({p95:.1f}%)"
    if running and room < RUNNING_MIN_ROOM_PCT:
        room = RUNNING_MIN_ROOM_PCT
        reason = "widened - coin is mid-move"
    return room, reason


def stop_price(price, room_pct, spread_pct, decimals):
    raw = price * (1 - room_pct / 100.0) * (1 - spread_pct / 100.0)
    return round(raw, decimals)


def fmt(value, decimals):
    return f"{value:,.{decimals}f}"


# --- Evaluation -------------------------------------------------------------

def evaluate(pos, price, candles):
    """Everything known about one position, with no side effects."""
    nights = overnight_drawdowns(candles)
    dds = [d for _, d in nights]
    p95 = percentile(dds, 0.95)
    running, cur_range, typ_range = intraday_regime(candles)
    room, reason = recommend_room(p95, running, pos.get("room_override_pct"))
    decimals = int(pos.get("decimals", 4))
    stop = stop_price(price, room, float(pos["spread_pct"]), decimals)
    qty = float(pos["qty"])
    return {
        "symbol": pos["symbol"],
        "price": price,
        "stop": stop,
        "room": room,
        "reason": reason,
        "risk": qty * (price - stop),
        "value": qty * price,
        "vs_cost": (stop / float(pos["avg_cost"]) - 1) * 100.0,
        "current_stop": pos.get("current_stop"),
        "nights": len(dds),
        "p95": p95,
        "worst": max(dds) if dds else None,
        "running": running,
        "range_6h": cur_range,
        "typical_6h": typ_range,
        "decimals": decimals,
    }


def alert_reason(ev, overnight):
    """Why this position deserves a notification - or None if it doesn't."""
    price, stop, live = ev["price"], ev["stop"], ev["current_stop"]
    if live is None:
        return "no stop set"
    if live >= price * (1 - TOO_CLOSE_PCT / 100.0):
        return "live stop is at/above the market - it will fire"
    drift = abs(stop - live) / price * 100.0
    bar = OVERNIGHT_DRIFT_PCT if overnight else DRIFT_PCT
    if drift > bar:
        direction = "too loose" if live < stop else "too tight"
        return f"live stop {direction} by {drift:.1f}% of price"
    if ev["running"]:
        return None
    return None


def suppressed(ev, state, now_ts):
    """True if this same advice went out recently and hasn't moved."""
    prev = state.get(ev["symbol"])
    if not prev:
        return False
    age_h = (now_ts - float(prev.get("ts", 0))) / 3600.0
    if age_h >= SUPPRESS_HOURS:
        return False
    moved = abs(ev["stop"] - float(prev.get("stop", 0))) / ev["price"] * 100.0
    return moved < MOVED_PCT


# --- Output -----------------------------------------------------------------

def push(title, message, priority="default", tags="lock", click=None):
    if not NTFY_TOPIC:
        log("NTFY_TOPIC is not set - printing instead of sending")
        log(f"  {title}\n{message}")
        return

    def ascii_only(s):
        return "".join(ch if 32 <= ord(ch) < 127 else "-" for ch in str(s))

    headers = {"Title": ascii_only(title)[:200],
               "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = ascii_only(click)
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=str(message).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 300:
                log(f"  ntfy returned {resp.status}")
    except Exception as exc:
        log(f"  push failed: {exc}")


def describe(ev, why):
    d = ev["decimals"]
    lines = [f"{ev['symbol']} @ ${fmt(ev['price'], d)}  ->  "
             f"stop ${fmt(ev['stop'], d)}"]
    live = ev["current_stop"]
    detail = f"  {ev['room']:.1f}% room, {ev['reason']}"
    if live is not None:
        detail += f" (live: ${fmt(live, d)})"
    lines.append(detail)
    lines.append(f"  risk ${ev['risk']:,.0f} | {ev['vs_cost']:+.1f}% vs cost")
    if ev["running"]:
        lines.append(f"  MID-MOVE: last 6h ranged {ev['range_6h']:.1f}% "
                     f"vs {ev['typical_6h']:.1f}% typical")
    lines.append(f"  why: {why}")
    return "\n".join(lines)


def main():
    cfg = json.loads(CONFIG_FILE.read_text())
    positions = cfg["positions"]

    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}

    force = os.environ.get("FORCE_ALERT") == "1"
    overnight = is_overnight()
    now_ts = datetime.now(timezone.utc).timestamp()
    log(f"checking {len(positions)} positions "
        f"({'overnight' if overnight else 'daytime'} thresholds)")

    evaluated, to_alert = [], []
    for pos in positions:
        product = pos["product"]
        price = fetch_price(product)
        if not price:
            log(f"  {pos['symbol']}: no price, skipping")
            continue
        try:
            candles = fetch_candles(product)
        except Exception as exc:
            log(f"  {pos['symbol']}: candles failed ({exc}), skipping")
            continue
        ev = evaluate(pos, price, candles)
        evaluated.append(ev)
        if ev["nights"] < MIN_NIGHTS:
            log(f"  {ev['symbol']}: only {ev['nights']} clean nights, "
                f"using floor width")
        why = alert_reason(ev, overnight)
        if force and not why:
            why = "manual run - full rundown"
        log(f"  {ev['symbol']}: ${fmt(price, ev['decimals'])} "
            f"-> ${fmt(ev['stop'], ev['decimals'])} "
            f"({ev['room']:.1f}% room){' RUNNING' if ev['running'] else ''}"
            f"{' | ' + why if why else ''}")
        if why and (force or not suppressed(ev, state, now_ts)):
            to_alert.append((ev, why))
        elif why:
            log(f"    suppressed - same advice sent within "
                f"{SUPPRESS_HOURS}h")

    if to_alert:
        symbols = ", ".join(ev["symbol"] for ev, _ in to_alert)
        total_risk = sum(ev["risk"] for ev in evaluated)
        total_value = sum(ev["value"] for ev in evaluated)
        body = "\n\n".join(describe(ev, why) for ev, why in to_alert)
        body += (f"\n\nBook ${total_value:,.0f} | "
                 f"at risk ${total_risk:,.0f} "
                 f"({total_risk / total_value * 100:.1f}%) at these levels")
        body += "\nCancel the old stop before entering the new one."
        body += ("\nPrices are Coinbase's; Robinhood's quote can sit "
                 "~0.5% either side, so re-check on the ticket.")
        push(
            title=f"Stops to update: {symbols}",
            message=body,
            priority="max" if overnight else "high",
            tags="lock",
            click="https://robinhood.com/crypto/" + to_alert[0][0]["symbol"],
        )
        for ev, _ in to_alert:
            state[ev["symbol"]] = {"stop": ev["stop"], "ts": now_ts}
        log(f"pushed: {symbols}")
    else:
        log("nothing to do - all stops within tolerance")

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
