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

Breakeven floor (added 2026-09-25, Kyle's rule): once the live stop already
sits at or above breakeven, the script never recommends moving it back below
breakeven. Breakeven is average cost grossed up for what a sale costs, so a
stop at the floor exits flat rather than at a small loss. Before the stop has
reached breakeven the floor does nothing - forcing a stop up to cost while the
price is only a few percent above it would put the stop inside ordinary noise.

Ratchet (added 2026-09-28, Kyle's rule): a stop only ever moves UP. The
measured room is re-priced from the live price every run, so after an
overnight drop it would happily recommend lowering every stop - which is
selling the insurance at the bottom, exactly when it is worth the most. Now
the script holds the stop Kyle already has whenever the measurement comes in
below it. It still says "raise it" when the price has run, and it still warns
when a stop is about to fire; the "too tight, lower it" alerts are gone. The
evidence: NEAR and WLD both filled overnight on 09-27/28 with the trail
intact, and the 60-day alert study (project doc "Alert filtering notes")
measured returns with a trailing-only stop.

Loud failures and order age (added 2026-10-16, Kyle's "stop safety"):
- A push is tried twice, and advice is marked sent only once ntfy accepts it.
  Before, a failed push was logged and the advice was still marked sent, so
  it stayed muted for SUPPRESS_HOURS.
- A lot that cannot be priced is listed by name, and the run exits non-zero,
  so the Actions run shows red instead of logging "all stops within
  tolerance" over coins it never checked.
- Robinhood cancels a good-til-canceled order 90 calendar days after it is
  placed (Robinhood Crypto glossary, checked 2026-10-15). Each lot carries
  "stop_placed", the date its live stop order was entered. From day 80 the
  check asks for the order to be re-entered at the same price, which resets
  the clock.
"""

import json
import math
import os
import statistics
import sys
import time
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

# Breakeven floor. Once the live stop is at or above breakeven, never
# recommend a stop below breakeven.
BREAKEVEN_FLOOR = True
# Ratchet. Never recommend a stop below the one already entered: the
# recommendation is max(measured stop, live stop). Set False to get the old
# "lower it after a drop" advice back.
RATCHET = True
# What a sale costs on top of the fill, by routing:
#   exchange      - itemized taker fee, charged on the sale notional. 0.95% is
#                   the $0-10K 30-day volume tier; it falls as volume rises.
#   market_maker  - no itemized fee; the spread is already in the fill price,
#                   and fills land at about the stop price.
#   unknown       - treated as exchange, the more conservative of the two.
EXCHANGE_TAKER_FEE_PCT = 0.95
# Fills land a touch under the stop (NEAR 2026-09-17: $3.40 -> $3.3942, 0.17%).
SLIPPAGE_PCT = 0.2
# What a filled stop costs, as the alerts show it (2026-10-17; Kyle: "i don't
# like thinking i'm losing $50 when i'm losing $500"). The old line printed
# qty x (price - stop) for each lot: how far today's price sat above the stop.
# At 7:04 PM CT Oct 17 the seven PYTH lots read about $65 together, and the
# stop had already filled at 5:19 PM for -$521 against what was paid. Every
# alert now leads with the loss against cost if the stop fills, per coin,
# with the fill this far under the stop price. Fills measured: PYTH Oct 17
# 0.96%, BAT Oct 15 0.29%, the five Oct 14 fills within 0.35%. The breakeven
# floor keeps SLIPPAGE_PCT; the loss shown uses the worse figure so it never
# understates.
FILL_SLIPPAGE_PCT = 1.0

# 30-day maximum hold (2026-10-18; Kyle: "If you think it'll help us make
# money then yes"). The replay behind the scheme's +3.9% a trade closed every
# position after 30 days (claude/money-maker-replay-2026-10-14.md); the live
# rules had no limit, and refereed crypto momentum fades by 2-4 weeks and
# reverses after about a month (Dobrynskaya 2023; Tzouvanas et al. 2020; Liu,
# Tsyvinski & Wu 2022). From day MAX_HOLD_DAYS after the lot's "bought" date,
# every stop check inside HOLD_SELL_HOURS (Central) says to sell that lot at
# market; the reminder repeats each check until the lot leaves positions.json.
MAX_HOLD_DAYS = 30
HOLD_SELL_HOURS = range(7, 19)          # 7 AM - 6:59 PM Central, like the buys
HOLD_SUPPRESS_HOURS = 3                 # one reminder per 4-hourly check

# Order age. Robinhood: "a GTC order will expire after 90 calendar days."
GTC_EXPIRY_DAYS = 90
EXPIRY_WARN_DAYS = 80
EXPIRY_SUPPRESS_HOURS = 24

# A failed push is tried once more after this pause.
PUSH_RETRY_WAIT_S = 3


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


RAISE_LOOKBACK_H = 12


def raise_reference(price, candles):
    """The price the trail is measured from: min(live, lowest close in the
    last RAISE_LOOKBACK_H hourly candles). Never above the live price."""
    closes = []
    for c in (candles or [])[:RAISE_LOOKBACK_H]:
        try:
            closes.append(float(c[4]))
        except (TypeError, ValueError, IndexError):
            continue
    if not closes:
        return price
    return min(price, min(closes))


def stop_price(price, room_pct, spread_pct, decimals):
    raw = price * (1 - room_pct / 100.0) * (1 - spread_pct / 100.0)
    return round(raw, decimals)


def sell_cost_pct(pos):
    """Percent of the sale lost to fees and slippage, for this position."""
    if pos.get("sell_fee_pct") is not None:
        fee = float(pos["sell_fee_pct"])
    elif pos.get("routing") == "market_maker":
        fee = 0.0
    else:
        fee = EXCHANGE_TAKER_FEE_PCT
    return fee + SLIPPAGE_PCT


def breakeven_stop(pos, decimals):
    """The lowest stop price that still exits at or above average cost.

    Rounded UP, so rounding can never be what turns a flat exit into a loss.
    """
    raw = float(pos["avg_cost"]) / (1 - sell_cost_pct(pos) / 100.0)
    scale = 10 ** decimals
    return math.ceil(round(raw * scale, 6)) / scale


def lock_price(breakeven, room_pct, spread_pct):
    """Market price at which the measured stop first reaches breakeven."""
    return breakeven / ((1 - room_pct / 100.0) * (1 - spread_pct / 100.0))


def fmt(value, decimals):
    return f"{value:,.{decimals}f}"


def base_symbol(symbol):
    """The coin a lot belongs to: 'PYTH#3' -> 'PYTH'."""
    return str(symbol or "").split("#")[0].upper()


def signed_usd(x):
    """-$521 / +$1,859 / $0, whole dollars."""
    r = round(x)
    if r == 0:
        return "$0"
    return f"{'-' if r < 0 else '+'}${abs(r):,}"


def coin_fills(evaluated):
    """Per coin, first-seen order: (coin, lots, cost, loss_if_filled), summed
    over every lot of that coin. A loss is positive; a locked gain negative."""
    out = {}
    for ev in evaluated or []:
        if ev.get("loss_if_filled") is None:
            continue
        c = out.setdefault(base_symbol(ev.get("symbol")), [0, 0.0, 0.0])
        c[0] += 1
        c[1] += float(ev.get("cost") or 0.0)
        c[2] += float(ev["loss_if_filled"])
    return [(k, v[0], v[1], v[2]) for k, v in out.items()]


# --- Evaluation -------------------------------------------------------------

def evaluate(pos, price, candles):
    """Everything known about one position, with no side effects."""
    nights = overnight_drawdowns(candles)
    dds = [d for _, d in nights]
    p95 = percentile(dds, 0.95)
    running, cur_range, typ_range = intraday_regime(candles)
    room, reason = recommend_room(p95, running, pos.get("room_override_pct"))
    decimals = int(pos.get("decimals", 4))
    spread = float(pos["spread_pct"])
    # RAISE REFERENCE (2026-10-06). Measure the trail from the lowest hourly
    # close of the last RAISE_LOOKBACK_H hours, not from the live price. A
    # spike then cannot pull the stop up into its own retrace zone. Hourly
    # replay, 33 Robinhood coins, Aug 30-Oct 6 2026, 15% trail, 14-day hold:
    # raise on every new high  +8.2%, 70% stopped out, 60% of those whipsaws
    # raise on the 12h low    +12.8%, 35% stopped out  (same ranking in both
    # halves of the window). ORCA 10-05: the live-price raise to 1.978 was
    # taken out by a thin-book print at 1.968; the 12h-low raise would have
    # left the stop at 1.861.
    ref = raise_reference(price, candles)
    measured = stop_price(ref, room, spread, decimals)
    breakeven = breakeven_stop(pos, decimals)
    live = pos.get("current_stop")
    # Locked = the stop he actually has entered already protects breakeven.
    locked = BREAKEVEN_FLOOR and live is not None and float(live) >= breakeven
    floored = locked and measured < breakeven
    stop = breakeven if floored else measured
    # Ratcheted = the stop he already has is higher than anything the
    # measurement (or the floor) would suggest. Hold it: stops only move up.
    ratcheted = RATCHET and live is not None and stop < float(live)
    if ratcheted:
        stop = float(live)
    qty = float(pos["qty"])
    cost = qty * float(pos["avg_cost"])
    return {
        "symbol": pos["symbol"],
        "price": price,
        "raise_ref": ref,
        "stop": stop,
        "measured_stop": measured,
        "breakeven": breakeven,
        "locked": locked,
        "floored": floored,
        "ratcheted": ratcheted,
        "lock_price": lock_price(breakeven, room, spread),
        "room": room,
        "reason": reason,
        # risk: how far today's price sits above the stop, in dollars. Not a
        # loss figure, and no longer shown as one; loss_if_filled is.
        "risk": qty * (price - stop),
        "cost": cost,
        "loss_if_filled": cost - qty * stop * (1 - FILL_SLIPPAGE_PCT / 100.0),
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
        if ev.get("floored") and not ev.get("ratcheted"):
            return ("price is back near breakeven - the floor is doing its "
                    "job; this stop exits flat if it fires")
        if ev.get("ratcheted"):
            return ("price is back down at your stop - it will fire; that is "
                    "the trail doing its job, nothing to change")
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


def stop_age_days(pos, today=None):
    """Whole days since the live stop order was placed, or None if unknown."""
    placed = pos.get("stop_placed")
    if not placed or pos.get("current_stop") is None:
        return None
    try:
        placed_day = datetime.strptime(str(placed), "%Y-%m-%d").date()
    except ValueError:
        return None
    if today is None:
        now = local_now()
        today = (now or datetime.now(timezone.utc)).date()
    return (today - placed_day).days


def expiry_reason(pos, today=None):
    """The order-age warning for one lot, or None while it is young enough."""
    age = stop_age_days(pos, today)
    if age is None or age < EXPIRY_WARN_DAYS:
        return None
    placed_day = datetime.strptime(str(pos["stop_placed"]), "%Y-%m-%d").date()
    expires = placed_day + timedelta(days=GTC_EXPIRY_DAYS)
    return (f"stop order is {age} days old - Robinhood cancels good-til-"
            f"canceled orders after {GTC_EXPIRY_DAYS} days (about "
            f"{expires:%b %d}); cancel it and re-enter it at the same price "
            f"to reset the clock")


def hold_age_days(pos, today=None):
    """Whole days since the lot was bought, or None if the date is missing."""
    bought = pos.get("bought")
    if not bought:
        return None
    try:
        bought_day = datetime.strptime(str(bought), "%Y-%m-%d").date()
    except ValueError:
        return None
    if today is None:
        now = local_now()
        today = (now or datetime.now(timezone.utc)).date()
    return (today - bought_day).days


def hold_limit_reason(pos, today=None, hour=None):
    """The 30-day sale instruction for one lot, or None while it is younger
    than MAX_HOLD_DAYS or the clock is outside HOLD_SELL_HOURS."""
    age = hold_age_days(pos, today)
    if age is None or age < MAX_HOLD_DAYS:
        return None
    if hour is None:
        now = local_now()
        hour = (now or datetime.now(timezone.utc)).hour
    if hour not in HOLD_SELL_HOURS:
        return None
    return (f"DAY {age}: the scheme's {MAX_HOLD_DAYS}-day limit (bought "
            f"{pos.get('bought')}) - sell this lot at market now, then cancel "
            f"its stop and tell Claude the fill")


def hold_suppressed(symbol, state, now_ts):
    """The 30-day sale reminder goes out once per stop check."""
    prev = state.get(f"hold:{symbol}")
    if not prev:
        return False
    age_h = (now_ts - float(prev.get("ts", 0))) / 3600.0
    return age_h < HOLD_SUPPRESS_HOURS


def is_hold_sale(why):
    return str(why or "").startswith("DAY ")


def expiry_suppressed(symbol, state, now_ts):
    """The order-age warning goes out at most once a day per lot."""
    prev = state.get(f"expiry:{symbol}")
    if not prev:
        return False
    age_h = (now_ts - float(prev.get("ts", 0))) / 3600.0
    return age_h < EXPIRY_SUPPRESS_HOURS


# --- Output -----------------------------------------------------------------

def push(title, message, priority="default", tags="lock", click=None):
    """Send one notification. True once ntfy accepts it, False if both tries
    fail (the caller then leaves the advice unmarked, so it is sent again)."""
    if not NTFY_TOPIC:
        log("NTFY_TOPIC is not set - printing instead of sending")
        log(f"  {title}\n{message}")
        return True

    def ascii_only(s):
        return "".join(ch if 32 <= ord(ch) < 127 else "-" for ch in str(s))

    headers = {"Title": ascii_only(title)[:200],
               "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = ascii_only(click)
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=str(message).encode("utf-8"), headers=headers, method="POST")
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status < 300:
                    return True
                log(f"  ntfy returned {resp.status} (try {attempt})")
        except Exception as exc:
            log(f"  push failed (try {attempt}): {exc}")
        if attempt == 1:
            time.sleep(PUSH_RETRY_WAIT_S)
    return False


def describe(ev, why):
    d = ev["decimals"]
    lines = [f"{ev['symbol']} @ ${fmt(ev['price'], d)}  ->  "
             f"stop ${fmt(ev['stop'], d)}"]
    live = ev["current_stop"]
    detail = f"  {ev['room']:.1f}% room, {ev['reason']}"
    if live is not None:
        detail += f" (live: ${fmt(live, d)})"
    lines.append(detail)
    if ev.get("loss_if_filled") is not None:
        lines.append(f"  if it fills: {signed_usd(-ev['loss_if_filled'])} against the "
                     f"${ev['cost']:,.0f} you paid (stop {ev['vs_cost']:+.1f}% vs "
                     f"cost, ~{FILL_SLIPPAGE_PCT:g}% fill slippage)")
    else:
        lines.append(f"  stop {ev['vs_cost']:+.1f}% vs cost")
    if ev.get("stop"):
        gap = (ev["price"] / ev["stop"] - 1) * 100.0
        lines.append(f"  price is {abs(gap):.1f}% {'above' if gap >= 0 else 'under'} "
                     f"the stop")
    if is_hold_sale(why) and ev.get("cost") is not None:
        qty = ev["value"] / ev["price"] if ev["price"] else 0.0
        proceeds = qty * ev["price"] * (1 - FILL_SLIPPAGE_PCT / 100.0)
        lines.append(f"  if sold now: {signed_usd(proceeds - ev['cost'])} against the "
                     f"${ev['cost']:,.0f} you paid (sell {qty:,.10g} at market)")
    if ev.get("ratcheted"):
        lines.append(f"  HELD at your live stop - the measured room would put "
                     f"it at ${fmt(ev['measured_stop'], d)}, but stops only "
                     f"move up")
    elif ev.get("floored"):
        lines.append(f"  FLOORED at breakeven ${fmt(ev['breakeven'], d)} - "
                     f"measured room alone says ${fmt(ev['measured_stop'], d)}. "
                     f"Tighter than the noise, on purpose: a dip takes you "
                     f"out flat.")
    if ev.get("locked"):
        if ev.get("ratcheted") or not ev.get("floored"):
            lines.append(f"  breakeven ${fmt(ev['breakeven'], d)} is locked in")
    elif "breakeven" in ev:
        lines.append(f"  breakeven ${fmt(ev['breakeven'], d)} - locks in once "
                     f"price reaches ~${fmt(ev['lock_price'], d)} and you "
                     f"raise the stop")
    if ev["running"]:
        lines.append(f"  MID-MOVE: last 6h ranged {ev['range_6h']:.1f}% "
                     f"vs {ev['typical_6h']:.1f}% typical")
    lines.append(f"  why: {why}")
    return "\n".join(lines)


def needs_action(why):
    """False for the one alert that asks for nothing: a stop about to fire."""
    return "will fire" not in why


def alert_title(to_alert, evaluated=None):
    """With the run's evaluated lots, an about-to-fire title carries what each
    coin loses against cost if its stop fills (every lot of the coin)."""
    symbols = ", ".join(ev["symbol"] for ev, _ in to_alert)
    sells = [ev["symbol"] for ev, why in to_alert if is_hold_sale(why)]
    if sells:
        rest = [ev["symbol"] for ev, why in to_alert if not is_hold_sale(why)]
        title = f"Sell today ({MAX_HOLD_DAYS}-day limit): {', '.join(sells)}"
        return title + (f"; stops to check: {', '.join(rest)}" if rest else "")
    if any(needs_action(why) for _, why in to_alert):
        return f"Stops to update: {symbols}"
    coins = list(dict.fromkeys(base_symbol(ev["symbol"]) for ev, _ in to_alert))
    fills = {c: (n, loss) for c, n, _, loss in coin_fills(evaluated)}
    if coins and all(c in fills for c in coins):
        parts = [f"{c}{f' ({fills[c][0]} lots)' if fills[c][0] > 1 else ''} "
                 f"{signed_usd(-fills[c][1])}" for c in coins]
        word = "Stop" if len(coins) == 1 else "Stops"
        return f"{word} about to fire: {', '.join(parts)} vs what you paid"
    return f"Stop about to fire: {symbols}"


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

    evaluated, to_alert, skipped, expiring, selling = [], [], [], set(), set()
    for pos in positions:
        product = pos["product"]
        price = fetch_price(product)
        if not price:
            log(f"  {pos['symbol']}: no price, skipping")
            skipped.append(pos["symbol"])
            continue
        try:
            candles = fetch_candles(product)
        except Exception as exc:
            log(f"  {pos['symbol']}: candles failed ({exc}), skipping")
            skipped.append(pos["symbol"])
            continue
        ev = evaluate(pos, price, candles)
        evaluated.append(ev)
        if ev["nights"] < MIN_NIGHTS:
            log(f"  {ev['symbol']}: only {ev['nights']} clean nights, "
                f"using floor width")
        why = alert_reason(ev, overnight)
        hold_why = hold_limit_reason(pos)
        if hold_why and (force or not hold_suppressed(ev["symbol"], state, now_ts)):
            why = hold_why + (f" | also: {why}" if why else "")
            selling.add(ev["symbol"])
        if not why:
            age_why = expiry_reason(pos)
            if age_why and (force or not expiry_suppressed(ev["symbol"], state, now_ts)):
                why = age_why
                expiring.add(ev["symbol"])
        if force and not why:
            why = "manual run - full rundown"
        log(f"  {ev['symbol']}: ${fmt(price, ev['decimals'])} "
            f"-> ${fmt(ev['stop'], ev['decimals'])} "
            f"({ev['room']:.1f}% room){' RUNNING' if ev['running'] else ''}"
            f"{' HELD' if ev['ratcheted'] else ' FLOORED' if ev['floored'] else ''}"
            f"{' locked' if ev['locked'] and not ev['floored'] else ''}"
            f"{' | ' + why if why else ''}")
        if why and (force or ev["symbol"] in expiring or ev["symbol"] in selling
                    or not suppressed(ev, state, now_ts)):
            to_alert.append((ev, why))
        elif why:
            log(f"    suppressed - same advice sent within "
                f"{SUPPRESS_HOURS}h")

    push_failed = False
    if to_alert:
        symbols = ", ".join(ev["symbol"] for ev, _ in to_alert)
        total_value = sum(ev["value"] for ev in evaluated)
        total_cost = sum(ev.get("cost") or 0.0 for ev in evaluated)
        total_loss = sum(ev.get("loss_if_filled") or 0.0 for ev in evaluated)
        body = "\n\n".join(describe(ev, why) for ev, why in to_alert)
        coins = list(dict.fromkeys(base_symbol(ev["symbol"]) for ev, _ in to_alert))
        fills = {c: (n, cost, loss) for c, n, cost, loss in coin_fills(evaluated)}
        per_coin = [f"  {c}{f', all {fills[c][0]} lots' if fills[c][0] > 1 else ''}: "
                    f"{signed_usd(-fills[c][2])} against ${fills[c][1]:,.0f} paid"
                    for c in coins if c in fills]
        if per_coin:
            body += ("\n\nIf these stops fill (vs what you paid, ~"
                     f"{FILL_SLIPPAGE_PCT:g}% fill slippage):\n" + "\n".join(per_coin))
        body += (f"\n\nBook ${total_value:,.0f} now, ${total_cost:,.0f} paid. "
                 f"If every stop fills: {signed_usd(-total_loss)} against what "
                 f"you paid.")
        if selling:
            body += (f"\n\nThe {MAX_HOLD_DAYS}-day limit: sell the named lot's quantity at "
                     f"market (market-maker routing), then cancel that lot's stop. The "
                     f"reminder repeats every check until the lot is removed from "
                     f"positions.json.")
        if any(needs_action(why) and not is_hold_sale(why) for _, why in to_alert):
            body += ("\nCancel the old stop before entering the new one, then move "
                     "that coin's Robinhood price alert to the new stop (and its "
                     "second alert to 5% under the coin's lowest stop).")
        body += ("\nPrices are Coinbase's; Robinhood's quote can sit "
                 "~0.5% either side, so re-check on the ticket.")
        if skipped:
            body += (f"\nNot checked (no price): {', '.join(skipped)}")
        sent = push(
            title=alert_title(to_alert, evaluated),
            message=body,
            priority="max" if overnight else "high",
            tags="moneybag" if selling else "lock",
            click="https://robinhood.com/crypto/" + to_alert[0][0]["symbol"].split("#")[0],
        )
        if sent:
            for ev, _ in to_alert:
                if ev["symbol"] in selling:
                    state[f"hold:{ev['symbol']}"] = {"ts": now_ts}
                elif ev["symbol"] in expiring:
                    state[f"expiry:{ev['symbol']}"] = {"ts": now_ts}
                else:
                    state[ev["symbol"]] = {"stop": ev["stop"], "ts": now_ts}
            log(f"pushed: {symbols}")
        else:
            push_failed = True
            log(f"push FAILED: {symbols} - left unmarked so the next run "
                f"sends it again")
    elif skipped:
        log(f"nothing to push for the {len(evaluated)} lots that were checked")
    else:
        log("nothing to do - all stops within tolerance")

    if skipped:
        log(f"{len(skipped)} of {len(positions)} lots NOT checked (no price): "
            f"{', '.join(skipped)}")
        if not evaluated:
            if not push("Stop check could not price any coin",
                        f"None of the {len(positions)} lots could be priced from "
                        f"Coinbase this run, so no stop was checked. The stop "
                        f"orders on Robinhood are unaffected.",
                        priority="high", tags="warning"):
                push_failed = True

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    if push_failed or skipped:
        log("exiting with an error so this run shows as failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
