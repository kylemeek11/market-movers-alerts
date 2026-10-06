"""
Daily context for the crypto alerts: the checklist, carried on the phone.

alerts_bot.py sees the market one five-minute slice at a time. The entry
checklist adopted on 2026-10-05/06 (claude/trading-strategy.md) is written in
DAYS - a tight 14-day base, the 50-day average, the 30-day high, 30 days of
daily volatility, what Bitcoin did over the last three days - so this module
keeps a once-a-day picture of every coin in the universe and turns it into:

1. BASE BREAKOUT - a standalone alert. The prior 14 UTC daily closes sat
   inside a 15% range, the coin is up 5%+ today against yesterday's close,
   it is above its 50-day average, and it is a weekday (UTC). Coinbase daily
   candles, 82 Robinhood coins, Jan-Oct 2026, 15% trailing stop, 14-day
   hold, 1.88% costs:
       tight base + 5% day, above 50-day, weekday   +1.5%  run 30% / stop 11%
       same, below the 50-day                         0.0%
       same, Saturday/Sunday                         -1.9%  run 18%
       mid or loose base + 5% day                    -1.7% / -2.1%  stop 22-30%
       every coin-day (baseline)                     -0.2% to -1.1%
   Same chance of a +20% run as a loose base, less than half the stop-outs.
   About two signals a day across the universe; a third of them overnight,
   which is when runs tend to start (47% begin 9pm-6am CT).

   One honest difference from the study: the study entered at the CLOSE of a
   +5% day; the live alert fires the moment the day's move crosses +5%, so a
   coin that fades back by the close was never in the sample. The alert says
   "today so far" for that reason.

2. CONTEXT LINES appended to every crypto alert (rate, threshold, breakout):
   - BTC down 3%+ in 24h: buys on BTC dump days stopped out 37% (vs 15%).
   - BTC down 5%+ over 3 days: -4.3%; a coin that stayed green through it
     -8.4% with 55% stopped out ("relative strength" catches down later).
   - A BTC -5% day inside the last 3 days: the day after averaged -10.7%
     with 71% stopped out, two days after -8.6%. No buys for 3 days.
   - BTC up 5%+ over 3 days and this coin under +1%: no catch-up trade
     (-2.1%, run 13%); coins already following BTC up ran 27% of the time.
   - Daily volatility over 5%/day: the wildest quarter of coins lost -1.6%
     under this stop (calmest quarter +1.2%). Half size.
   - Bitstamp depth. Robinhood's exchange routing sends the order to
     Bitstamp's book and the stop watches Bitstamp's bid; ORCA's stop on
     2026-10-05 tripped on an empty book and filled 14% below every other
     venue. Rule: exchange routing only where bids within 15% of mid total
     $100K+; otherwise market-maker routing for the stop.

3. An overnight pass for the two green signals. Between 10pm and 7am CT the
   bot holds ordinary alerts so a Sleep Focus can let ntfy through safely.
   TOP SETUP rate alerts and BASE BREAKOUTs are the alerts the checklist
   says to act on, and runs start overnight, so they go through - at most
   NIGHT_PASS_MAX per night.

Data: one Coinbase daily-candle request per coin per UTC day (spread across
runs, DAILY_FETCH_PER_RUN at a time), cached in the state file under
"daily". Coins Coinbase does not list (MEW, MNT, ...) get no daily context
and no breakout alert; everything else about them is unchanged. The Bitstamp
pair list is fetched once a day; an order book is read only when an alert is
actually going out.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

COINBASE_API = "https://api.exchange.coinbase.com"
BITSTAMP_API = "https://www.bitstamp.net/api/v2"
TIMEOUT = 10

# --- The checklist numbers (claude/trading-strategy.md) ----------------------
BASE_DAYS = 14                 # the base is the prior 14 completed daily closes
BASE_RANGE_MAX_PCT = 15.0      # ...kept inside this high-to-low range
BREAKOUT_DAY_PCT = 5.0         # today's move against yesterday's close
TREND_DAYS = 50                # above the average of this many closes
HIGH_DAYS = 30                 # distance from the high over this many days
VOL_DAYS = 30                  # mean absolute daily move over this many days
HIGH_VOL_PCT = 5.0             # yellow flag above this
DEEP_DIP_PCT = 20.0            # red: more than this below the 30-day high

BTC_DAY_DROP_PCT = 3.0         # red: BTC down this much in 24h
BTC_3D_DROP_PCT = 5.0          # red: BTC down this much over 3 days
BTC_DUMP_DAY_PCT = 5.0         # a BTC day this bad...
BTC_DUMP_WAIT_DAYS = 3         # ...means no buys for this many days
BTC_3D_RISE_PCT = 5.0          # "no catch-up trade" context
LAGGARD_3D_PCT = 1.0           # a coin under this after a BTC rise has not followed

THIN_BOOK_USD = 100_000.0      # Bitstamp bids within 15% of mid below this: thin
BOOK_DEPTH_PCT = 15.0

BREAKOUT_CONFIRM_MIN = 10.0    # the +5% must still be there this many minutes later
DAILY_FETCH_PER_RUN = 45       # candle requests per run while the day's cache fills
DAILY_HISTORY_DAYS = 60        # enough for the 50-day average with a margin
REQUEST_GAP_SEC = 0.15         # public endpoint; don't hammer it
NIGHT_PASS_MAX = 2             # overnight wake-ups for green signals, per night

MIN_DAYS_FOR_BASE = BASE_DAYS + 1      # yesterday's close plus the base behind it
MIN_DAYS_FOR_TREND = 30                # fewer closes than this -> no trend opinion


def _log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def _get_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "market-movers-alerts/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def utc_day(ts=None):
    when = (datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None
            else datetime.now(timezone.utc))
    return when.strftime("%Y-%m-%d")


def is_weekend(ts=None):
    """Saturday or Sunday by the UTC calendar - the calendar the study used."""
    when = (datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None
            else datetime.now(timezone.utc))
    return when.weekday() >= 5


# --- Building one coin's daily picture ---------------------------------------

def summarize(candles, now_ts=None):
    """Turn Coinbase daily candles into the numbers the checklist needs.

    `candles` are [time, low, high, open, close, volume] in any order. The
    candle for the current UTC day is in progress and is kept separate from
    the completed ones: the base, the trend average and the volatility are
    measured on completed days only, like the backtest; today's intraday high
    joins the 30-day high.

    Returns None when there is not enough history to say anything.
    """
    today = utc_day(now_ts)
    now = (datetime.fromtimestamp(now_ts, tz=timezone.utc) if now_ts is not None
           else datetime.now(timezone.utc))
    today_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = []
    for c in candles or []:
        try:
            rows.append((int(c[0]), float(c[1]), float(c[2]), float(c[3]),
                         float(c[4])))
        except (TypeError, ValueError, IndexError):
            continue
    rows = [r for r in rows if r[0] < today_start + 86400]   # nothing from the future
    rows.sort(key=lambda r: -r[0])          # newest first
    completed = [r for r in rows if r[0] < today_start]
    if len(completed) < MIN_DAYS_FOR_BASE:
        return None
    closes = [r[4] for r in completed]
    base = closes[:BASE_DAYS]
    lo, hi = min(base), max(base)
    out = {
        "d": today,
        "prev_close": closes[0],
        "base14": (hi / lo - 1) * 100.0 if lo > 0 else None,
        "closes": closes[:TREND_DAYS],
        "high30": max(r[2] for r in rows[:HIGH_DAYS]) if rows else None,
        "close3": closes[2] if len(closes) > 2 else None,    # 3 days ago
    }
    trend = closes[:TREND_DAYS]
    out["avg50"] = (sum(trend) / len(trend)) if len(trend) >= MIN_DAYS_FOR_TREND else None
    moves = []
    for i in range(min(VOL_DAYS, len(closes) - 1)):
        if closes[i + 1] > 0:
            moves.append(abs(closes[i] / closes[i + 1] - 1) * 100.0)
    out["vol30"] = (sum(moves) / len(moves)) if len(moves) >= 10 else None
    # Daily returns of the last few completed days, newest first - for the
    # BTC "dump day" rule.
    out["days"] = [(closes[i] / closes[i + 1] - 1) * 100.0
                   for i in range(min(5, len(closes) - 1)) if closes[i + 1] > 0]
    return out


def fetch_daily(symbol, now_ts=None, days=DAILY_HISTORY_DAYS):
    """Coinbase daily candles for SYMBOL-USD, or None (404 -> 'missing')."""
    now = (datetime.fromtimestamp(now_ts, tz=timezone.utc) if now_ts is not None
           else datetime.now(timezone.utc))
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=days + 1)
    url = (f"{COINBASE_API}/products/{quote(str(symbol).upper(), safe='')}-USD/candles"
           f"?granularity=86400&start={start:%Y-%m-%dT%H:%M:%SZ}"
           f"&end={end:%Y-%m-%dT%H:%M:%SZ}")
    try:
        data = _get_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "missing"
        return None
    except Exception:
        return None
    return data if isinstance(data, list) else None


def refresh(state, rows, now_ts=None, fetch=fetch_daily, budget=DAILY_FETCH_PER_RUN,
            log=_log, sleep=time.sleep):
    """Bring the per-coin daily cache up to today, a few coins per run.

    `rows` are the crypto rows from screen_crypto (the universe). Entries
    dated today are kept; stale ones are refetched, BTC first because the
    market lines depend on it. Returns the number of coins fetched.
    """
    now_ts = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    today = utc_day(now_ts)
    daily = state.setdefault("daily", {})
    missing = state.setdefault("cb_missing", {})
    wanted = ["BTC"] + sorted({str(r.get("symbol") or "").upper() for r in rows
                               if r.get("kind") == "crypto"} - {"BTC"})
    todo = [s for s in wanted if s and s not in missing
            and (daily.get(s) or {}).get("d") != today]
    if not todo:
        return 0
    fetched = 0
    for sym in todo[:budget]:
        data = fetch(sym, now_ts)
        fetched += 1
        if data == "missing":
            missing[sym] = True
            daily.pop(sym, None)
        elif data is None:
            continue                       # try again next run
        else:
            summary = summarize(data, now_ts)
            if summary is None:
                # Too young a listing to have a base. Remember the day so it
                # is not refetched every five minutes.
                daily[sym] = {"d": today, "short": True}
            else:
                daily[sym] = summary
        sleep(REQUEST_GAP_SEC)
    left = len(todo) - fetched
    log(f"  daily context: refreshed {fetched} coins"
        + (f", {left} to go next run" if left > 0 else ""))
    # Forget coins that have left the universe.
    for sym in [s for s in daily if s not in wanted and not s.startswith("_")]:
        del daily[sym]
    return fetched


# --- Reading the picture ----------------------------------------------------

def coin_context(entry, price, today=None):
    """Today's checklist numbers for one coin, from its cache entry and the
    live price. None when there is no usable entry - including one left over
    from yesterday (pass `today`), whose prev_close would be a day stale."""
    if not entry or entry.get("short") or not price or price <= 0:
        return None
    if today is not None and entry.get("d") != today:
        return None
    prev = entry.get("prev_close")
    if not prev or prev <= 0:
        return None
    ctx = {
        "day_pct": (price / prev - 1) * 100.0,
        "base14": entry.get("base14"),
        "vol30": entry.get("vol30"),
    }
    avg = entry.get("avg50")
    ctx["vs50_pct"] = (price / avg - 1) * 100.0 if avg else None
    high = entry.get("high30")
    high = max(high, price) if high else price
    ctx["off_high_pct"] = (price / high - 1) * 100.0 if high else None
    c3 = entry.get("close3")
    ctx["ret3_pct"] = (price / c3 - 1) * 100.0 if c3 else None
    return ctx


def btc_context(state, btc_price, today=None):
    """What Bitcoin has done: 24h, 3 days, and the last few daily returns."""
    entry = (state.get("daily") or {}).get("BTC")
    ctx = coin_context(entry, btc_price, today) if entry else None
    if ctx is None:
        return None
    return {"day_pct": ctx["day_pct"], "ret3_pct": ctx["ret3_pct"],
            "days": list(entry.get("days") or [])}


def is_breakout(ctx, now_ts=None):
    """The green-light test. Returns (green, reason)."""
    if ctx is None:
        return False, "no daily history"
    if ctx.get("base14") is None or ctx["base14"] > BASE_RANGE_MAX_PCT:
        return False, "base not tight"
    if ctx["day_pct"] < BREAKOUT_DAY_PCT:
        return False, "no +5% day"
    if ctx.get("vs50_pct") is None:
        return False, "no 50-day average"
    if ctx["vs50_pct"] <= 0:
        return False, "below 50-day average"
    if is_weekend(now_ts):
        return False, "weekend"
    return True, "tight base, +5% day, above 50-day, weekday"


def btc_lines(btc, coin_ctx=None):
    """The Bitcoin red lights, as alert body lines. Empty when BTC is fine."""
    lines = []
    if not btc:
        return lines
    day = btc.get("day_pct")
    r3 = btc.get("ret3_pct")
    if day is not None and day <= -BTC_DAY_DROP_PCT:
        lines.append(f"RED: BTC {day:+.1f}% in 24h - buys on BTC dump days "
                     f"stopped out 37% (15% normally)")
    if r3 is not None and r3 <= -BTC_3D_DROP_PCT:
        line = f"RED: BTC {r3:+.1f}% over 3 days - coins bought here averaged -4%"
        if coin_ctx and coin_ctx.get("ret3_pct") is not None and coin_ctx["ret3_pct"] > 0:
            line += (f"; this one held up ({coin_ctx['ret3_pct']:+.1f}%), and those "
                     f"catch down later (-8%, 55% stopped)")
        lines.append(line)
    days = btc.get("days") or []
    for back, ret in enumerate(days[:BTC_DUMP_WAIT_DAYS], start=1):
        if ret <= -BTC_DUMP_DAY_PCT:
            ago = "yesterday" if back == 1 else f"{back} days ago"
            lines.append(f"RED: BTC fell {ret:.0f}% {ago} - the day after a BTC -5% "
                         f"day averaged -10.7% with 71% stopped out; wait "
                         f"{BTC_DUMP_WAIT_DAYS} days")
            break
    if (r3 is not None and r3 >= BTC_3D_RISE_PCT and coin_ctx
            and coin_ctx.get("ret3_pct") is not None
            and coin_ctx["ret3_pct"] < LAGGARD_3D_PCT):
        lines.append(f"no catch-up trade: BTC {r3:+.0f}% over 3 days and this coin "
                     f"{coin_ctx['ret3_pct']:+.1f}% - laggards averaged -2.1%")
    return lines


def coin_lines(ctx):
    """The coin's own checklist lines: volatility and the deep-dip test."""
    lines = []
    if not ctx:
        return lines
    vol = ctx.get("vol30")
    if vol is not None and vol > HIGH_VOL_PCT:
        lines.append(f"high-vol coin ({vol:.1f}%/day): runs more often, stopped out "
                     f"more, net a loser - half size")
    off = ctx.get("off_high_pct")
    if off is not None and off <= -DEEP_DIP_PCT:
        lines.append(f"RED: {-off:.0f}% below its 30-day high - deep dips are not "
                     f"bargains (-2.4%, stop 19%)")
    return lines


def context_lines(state, row, btc_price=None, now_ts=None):
    """Everything the daily picture has to say about one alerting crypto row."""
    if row.get("kind") != "crypto":
        return []
    today = utc_day(now_ts)
    daily = state.get("daily") or {}
    sym = str(row.get("symbol") or "").upper()
    ctx = coin_context(daily.get(sym), row.get("price"), today)
    btc = btc_context(state, btc_price, today) if btc_price else None
    return btc_lines(btc, ctx) + coin_lines(ctx)


# --- Bitstamp depth ---------------------------------------------------------

def bitstamp_pairs(state, now_ts=None, fetch=None, log=_log):
    """{SYMBOL: url_symbol} for Bitstamp's USD pairs, cached for the day."""
    today = utc_day(now_ts)
    daily = state.setdefault("daily", {})
    cached = daily.get("_bitstamp")
    if cached and cached.get("d") == today and isinstance(cached.get("pairs"), dict):
        return cached["pairs"]
    try:
        data = (fetch or _get_json)(f"{BITSTAMP_API}/trading-pairs-info/")
    except Exception as exc:
        log(f"  Bitstamp pair list unavailable ({exc})")
        return cached.get("pairs") if cached else None
    pairs = {}
    for p in data if isinstance(data, list) else []:
        name = str(p.get("name") or "")
        if "/" not in name or not name.endswith("/USD"):
            continue
        if str(p.get("trading") or "").lower() not in ("enabled", ""):
            continue
        pairs[name.split("/")[0].upper()] = str(p.get("url_symbol") or "").lower()
    if pairs:
        daily["_bitstamp"] = {"d": today, "pairs": pairs}
    return pairs or (cached.get("pairs") if cached else None)


def book_depth_usd(book, pct=BOOK_DEPTH_PCT):
    """Dollars of bids within `pct` of the mid, from a Bitstamp order book."""
    try:
        bids = [(float(p), float(a)) for p, a in book.get("bids") or []]
        asks = [(float(p), float(a)) for p, a in book.get("asks") or []]
    except (TypeError, ValueError):
        return None
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2.0
    floor = mid * (1 - pct / 100.0)
    return sum(p * a for p, a in bids if p >= floor)


def thin_book_line(state, symbol, now_ts=None, fetch=None, log=_log):
    """One line on Bitstamp depth for the stop's routing, or None if unknown."""
    pairs = bitstamp_pairs(state, now_ts, fetch=fetch, log=log)
    if pairs is None:
        return None
    sym = str(symbol).upper()
    pair = pairs.get(sym)
    if not pair:
        return "not on Bitstamp - market-maker routing only for the stop"
    try:
        book = (fetch or _get_json)(f"{BITSTAMP_API}/order_book/{pair}/")
    except Exception:
        return None
    depth = book_depth_usd(book)
    if depth is None:
        return None
    if depth < THIN_BOOK_USD:
        return (f"thin book: ${depth / 1e3:,.0f}K of Bitstamp bids within 15% - "
                f"use market-maker routing for the stop")
    return f"Bitstamp bids within 15%: ${depth / 1e6:,.1f}M - exchange routing OK"


# --- The breakout alert -----------------------------------------------------

def collect_breakouts(state, rows, fired, now_ts):
    """Rows that are a green-light breakout and have not alerted today.

    The study's trigger is a CLOSE 5% up, which the live bot cannot wait
    for, so the next best thing: the move has to still be there
    BREAKOUT_CONFIRM_MIN later (two runs). HYPE on 2026-10-05 touched +5.3%
    for one five-minute slice and closed +4.1%; a one-slice wick is not a
    breakout. First sightings are remembered in state["base_seen"] and
    forgotten the moment the coin drops back under the bar.

    Returns [(fired_key, row, ctx)], biggest day move first.
    """
    today = utc_day(now_ts)
    daily = state.get("daily") or {}
    seen = state.setdefault("base_seen", {})
    out, still_green = [], set()
    for r in rows:
        if r.get("kind") != "crypto":
            continue
        sym = str(r.get("symbol") or "").upper()
        ctx = coin_context(daily.get(sym), r.get("price"), today)
        green, _ = is_breakout(ctx, now_ts)
        if not green:
            continue
        still_green.add(sym)
        key = f"base:crypto:{sym}"
        if fired.get(key) == today:
            continue
        first = seen.get(sym)
        if not isinstance(first, (int, float)) or first > now_ts:
            seen[sym] = now_ts
            continue
        if now_ts - first < BREAKOUT_CONFIRM_MIN * 60.0:
            continue
        out.append((key, r, ctx))
    for sym in [k for k in seen if k not in still_green]:
        del seen[sym]
    out.sort(key=lambda t: -t[2]["day_pct"])
    return out


def breakout_lines(row, ctx):
    """Body lines for a BASE BREAKOUT alert, before the shared context lines."""
    lines = [f"BASE BREAKOUT: +{ctx['day_pct']:.1f}% today so far from a "
             f"{ctx['base14']:.0f}% 14-day base, {ctx['vs50_pct']:+.0f}% vs 50-day avg"]
    bits = []
    if ctx.get("off_high_pct") is not None:
        bits.append(f"{-ctx['off_high_pct']:.0f}% below 30-day high")
    if ctx.get("vol30") is not None:
        bits.append(f"daily vol {ctx['vol30']:.1f}%")
    if bits:
        lines.append("  -  ".join(bits))
    lines.append("tight-base breakouts: +1.5% avg, run 30% / stop 11% "
                 "(loose base: stop 30%)")
    return lines


# --- Overnight pass for green signals -----------------------------------------

def night_id(now_ts, tz_name="America/Chicago", start_hour=22, end_hour=7):
    """The local date the current night belongs to, or None in the daytime."""
    when = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        when = when.astimezone(ZoneInfo(tz_name))
    except Exception:
        return None
    if when.hour >= start_hour:
        return when.strftime("%Y-%m-%d")
    if when.hour < end_hour:
        return (when - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def night_pass(state, now_ts, limit=NIGHT_PASS_MAX):
    """May one more green signal go out tonight? Counts it if so."""
    night = night_id(now_ts)
    if night is None:
        return True
    rec = state.setdefault("night_pass", {})
    if rec.get("night") != night:
        rec.clear()
        rec.update({"night": night, "count": 0})
    if rec["count"] >= limit:
        return False
    rec["count"] += 1
    return True
