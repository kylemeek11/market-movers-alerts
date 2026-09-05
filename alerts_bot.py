#!/usr/bin/env python3
"""
Market Movers phone alerts.

Runs on GitHub Actions, independent of any Mac. Screens the US stock market
and the crypto market for names up past a set of thresholds, and pushes a
notification to your phone via ntfy when one crosses a level it hasn't
crossed today. Each alert links straight to that symbol on Robinhood.

Stocks are screened during market hours only; crypto trades around the
clock, so it is screened on every run; overnight the phone's own Do Not
Disturb does the silencing, and only the biggest movers ask to break through.

Deliberately light: stocks need only the screener's quote payload and crypto
is one CoinGecko call, so a run finishes in seconds.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Stock settings (match market_movers.py) --------------------------------
MIN_GAIN_PCT = 5.0
MIN_DOLLAR_VOLUME = 25_000_000
MIN_MARKET_CAP = 100_000_000
MIN_PRICE = 3.00
MIN_DAY_VOLUME = 100_000

ALERT_LEVELS = [10.0, 15.0, 20.0, 30.0, 50.0]

# --- Crypto settings --------------------------------------------------------
# Crypto is far more volatile than equities, so the thresholds start higher:
# a 10% day is unremarkable for a coin and would just be noise.
CRYPTO_ALERT_LEVELS = [15.0, 25.0, 40.0, 60.0, 100.0]
CRYPTO_MIN_24H_VOLUME = 10_000_000     # USD traded in 24h
CRYPTO_MIN_MARKET_CAP = 50_000_000
CRYPTO_TOP_N = 250                     # how many coins by market cap to watch

# Velocity: what is moving RIGHT NOW, rather than what has already moved.
# A coin up sharply in the last hour while its 24h number is still modest is
# early in a run; the same hourly jump on top of +95% is the tail of one.
CRYPTO_VELOCITY_1H = 6.0               # up this much in the last hour
CRYPTO_VELOCITY_MAX_24H = 25.0         # but not already run further than this
VELOCITY_COOLDOWN_SEC = 4 * 3600       # re-alert the same name at most this often

# Relative volume: unusual participation usually shows up before the big
# price move does. Today's volume is projected to a full session and compared
# against the 3-month average.
RELVOL_MIN = 3.0                       # projected volume vs normal
RELVOL_MIN_GAIN = 3.0                  # and it has to actually be rising
SESSION_OPEN_MIN = 9 * 60 + 30         # 9:30 ET
SESSION_MINUTES = 390                  # 6.5h regular session

# Robinhood gate: there is no public API for what Robinhood lists, so the alert
# link itself is the check. A 404 means Robinhood has never heard of it - but a
# 200 is NOT enough, because Robinhood publishes price pages for plenty of
# coins it will not let you trade (DASH and UAI both render fine and are both
# untradable). The page embeds the real answer as a "tradability" field, and
# the first occurrence is the page's own asset, so that is what we read.
# Only candidates that already cleared a threshold are checked, and each answer
# is cached for the day, so this stays a handful of requests.
CHECK_ROBINHOOD = True
ROBINHOOD_TIMEOUT = 12
ROBINHOOD_SCAN_BYTES = 600_000   # the field sits ~200KB in; no need for the rest
TRADABILITY_RE = re.compile(rb'"tradability"\s*:\s*"([a-z_]+)"', re.I)
# Bump when the tradability logic changes, so answers cached by an older and
# possibly wrong version of the gate are thrown away instead of being trusted.
GATE_VERSION = 2

# --- Shared -----------------------------------------------------------------
MAX_ALERTS_PER_RUN = 6
HIGH_PRIORITY_LEVEL = 20.0             # stocks at/above this break through silence
CRYPTO_HIGH_PRIORITY_LEVEL = 40.0      # crypto has to move harder to do the same

MARKET_TZ = "America/New_York"
MARKET_OPEN_HOUR = 8
MARKET_CLOSE_HOUR = 17

# Overnight the bot does the filtering, because the phone cannot. iOS lets you
# allow an app through a Focus, but it is all-or-nothing - notification
# priority does not enter into it. So between OVERNIGHT_START_HOUR and
# OVERNIGHT_END_HOUR only movers past the high-priority threshold are sent at
# all, and they go at max priority. Anything smaller is held back WITHOUT
# being marked as fired, so it alerts normally after 7am if it still
# qualifies. That way ntfy can be allowed through a Sleep Focus safely: the
# only thing that can wake you is something genuinely big.
USER_TZ = "America/Chicago"
OVERNIGHT_START_HOUR = 22
OVERNIGHT_END_HOUR = 7

STATE_FILE = Path(os.environ.get("STATE_FILE", "state/alerts.json"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&order=market_cap_desc"
    f"&per_page={CRYPTO_TOP_N}&page=1&price_change_percentage=1h,24h"
)


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def local_now(tz_name):
    """Current time in the named zone, or None if the lookup fails."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    except Exception:
        return None


# ---------------------------------------------------------------------------

def market_is_open():
    now = local_now(MARKET_TZ)
    if now is None:
        return True          # if the timezone lookup fails, don't go silent
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN_HOUR <= now.hour < MARKET_CLOSE_HOUR


def in_overnight():
    now = local_now(USER_TZ)
    if now is None:
        return False         # if the lookup fails, behave like daytime
    return now.hour >= OVERNIGHT_START_HOUR or now.hour < OVERNIGHT_END_HOUR


def priority_for(level, high_level, overnight):
    """Day: default, stepping up to high past the threshold.
    Night: only the big ones are sent at all, and they go at max.
    """
    if level >= high_level:
        return "max" if overnight else "high"
    return "default"


def session_fraction():
    """How much of the regular session has elapsed, or None if it is shut.

    Volume-so-far is meaningless against a full-day average unless you
    normalise for the time of day, so relative volume is only computed
    inside the regular session.
    """
    now = local_now(MARKET_TZ)
    if now is None or now.weekday() >= 5:
        return None
    mins = (now.hour * 60 + now.minute) - SESSION_OPEN_MIN
    if mins <= 0 or mins > SESSION_MINUTES:
        return None
    # Floor the divisor: in the first few minutes the projection is wild.
    return max(mins / float(SESSION_MINUTES), 0.10)


def push(title, message, priority="default", tags="chart_with_upwards_trend",
         click=None):
    """Send one notification. Headers must be ASCII; the body may be UTF-8."""
    import urllib.request

    if not NTFY_TOPIC:
        log("NTFY_TOPIC is not set - printing instead of sending")
        log(f"  {title} | {message}")
        return

    def ascii_only(s):
        return "".join(ch if 32 <= ord(ch) < 127 else "-" for ch in str(s))

    headers = {
        "Title": ascii_only(title)[:200],
        "Priority": priority,
        "Tags": tags,
    }
    if click:
        # Makes the whole notification tappable - straight to Robinhood.
        headers["Click"] = ascii_only(click)

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=str(message).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 300:
                log(f"  ntfy returned {resp.status}")
    except Exception as exc:
        log(f"  push failed: {exc}")


# --- Screens ----------------------------------------------------------------

def screen_stocks(yf, floor):
    from yfinance import EquityQuery

    def build(with_cap):
        terms = [
            EquityQuery("gt", ["percentchange", floor]),
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
            log(f"  stock screener failed: {exc}")
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
        if chg is None or chg < floor:
            continue
        if price < MIN_PRICE or vol < MIN_DAY_VOLUME:
            continue
        if (q.get("marketCap") or 0) < MIN_MARKET_CAP:
            continue
        if price * vol < MIN_DOLLAR_VOLUME:
            continue
        out.append({
            "kind": "stock",
            "symbol": q["symbol"],
            "name": q.get("shortName") or q.get("longName") or q["symbol"],
            "pct": float(chg),
            "price": float(price),
            "dollars": float(price) * float(vol),
            "hour_pct": None,
            "volume": float(vol),
            "avg_volume": float(q.get("averageDailyVolume3Month")
                                or q.get("averageDailyVolume10Day") or 0),
        })
    out.sort(key=lambda r: -r["pct"])
    return out


def screen_crypto():
    """Top coins by market cap, filtered to real movers on real volume."""
    import urllib.request

    req = urllib.request.Request(
        COINGECKO_URL,
        headers={"Accept": "application/json",
                 "User-Agent": "market-movers-alerts/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            coins = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"  crypto screener failed: {exc}")
        return []

    if not isinstance(coins, list):
        log("  crypto screener returned an unexpected payload")
        return []

    out = []
    for c in coins:
        sym = (c.get("symbol") or "").upper()
        if not sym:
            continue
        chg = c.get("price_change_percentage_24h_in_currency")
        if chg is None:
            chg = c.get("price_change_percentage_24h")
        price = c.get("current_price") or 0
        vol = c.get("total_volume") or 0
        cap = c.get("market_cap") or 0
        if chg is None:
            continue
        hour = c.get("price_change_percentage_1h_in_currency")
        big_enough = float(chg) >= min(CRYPTO_ALERT_LEVELS)
        moving_now = hour is not None and float(hour) >= CRYPTO_VELOCITY_1H
        if not (big_enough or moving_now):
            continue
        if vol < CRYPTO_MIN_24H_VOLUME or cap < CRYPTO_MIN_MARKET_CAP:
            continue
        out.append({
            "kind": "crypto",
            "symbol": sym,
            "name": c.get("name") or sym,
            "pct": float(chg),
            "price": float(price),
            "dollars": float(vol),
            "hour_pct": hour,
        })
    out.sort(key=lambda r: -r["pct"])
    return out


# --- State ------------------------------------------------------------------

def load_state():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        state = json.loads(STATE_FILE.read_text())
        if state.get("date") == today:
            state.setdefault("tradable", {})
            if state.get("gate") != GATE_VERSION:
                log("  tradability cache came from an older gate - clearing")
                state["tradable"] = {}
                state["gate"] = GATE_VERSION
            return state
        log("  state is from a previous day - starting fresh")
    except (OSError, ValueError):
        log("  no previous state found")
    return {"date": today, "fired": {}, "tradable": {},
            "gate": GATE_VERSION}


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except OSError as exc:
        log(f"  could not save state: {exc}")


# --- Alerting ---------------------------------------------------------------

def robinhood_url(row):
    if row["kind"] == "crypto":
        return f"https://robinhood.com/crypto/{row['symbol']}"
    return f"https://robinhood.com/stocks/{row['symbol']}"


def robinhood_tradable(row, cache):
    """Is this symbol actually buyable on Robinhood?

    Cached per symbol for the life of the state file (one day). On any
    network trouble we return True: a false positive is a wasted tap, a
    false negative is a missed run, and the missed run is the worse error.
    """
    if not CHECK_ROBINHOOD:
        return True
    key = f"{row['kind']}:{row['symbol']}"
    if key in cache:
        return cache[key]

    import urllib.request
    import urllib.error

    req = urllib.request.Request(
        robinhood_url(row),
        headers={"User-Agent": "Mozilla/5.0 (compatible; market-movers-alerts/1.0)",
                 "Accept-Encoding": "identity"},   # keeps the partial read readable
    )
    try:
        with urllib.request.urlopen(req, timeout=ROBINHOOD_TIMEOUT) as resp:
            head = resp.read(ROBINHOOD_SCAN_BYTES)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            cache[key] = False       # Robinhood has never heard of it
            log(f"  skip {row['symbol']} - no Robinhood listing (404)")
            return False
        log(f"  Robinhood check for {row['symbol']} returned {exc.code} - allowing")
        return True
    except Exception as exc:
        log(f"  Robinhood check failed for {row['symbol']} ({exc}) - allowing")
        return True                  # do not go silent on a network blip

    match = TRADABILITY_RE.search(head)
    if not match:
        # Page loaded but the field moved or is missing. Fail open and say so,
        # rather than silently muting a real run.
        log(f"  Robinhood tradability not found for {row['symbol']} - allowing")
        return True

    ok = match.group(1).lower() == b"tradable"
    cache[key] = ok
    if not ok:
        log(f"  skip {row['symbol']} - Robinhood says "
            f"{match.group(1).decode('ascii', 'replace')}")
    return ok


def filter_tradable(items, cache, row_index):
    """Keep only entries whose symbol Robinhood actually carries."""
    return [it for it in items if robinhood_tradable(it[row_index], cache)]


def format_body(row):
    price = row["price"]
    price_str = f"${price:,.2f}" if price >= 1 else f"${price:,.6f}".rstrip("0")
    parts = [f"now +{row['pct']:.1f}%", price_str]
    if row["hour_pct"] is not None:
        parts.append(f"1h {row['hour_pct']:+.1f}%")
    line2 = "  -  ".join(parts)
    if row["kind"] == "crypto":
        line3 = f"${row['dollars'] / 1e6:,.0f}M 24h volume"
    else:
        line3 = f"${row['dollars'] / 1e6:,.0f}M traded"
    return f"{row['name']}\n{line2}\n{line3}"


def collect_velocity(rows, fired, now_ts):
    """Coins moving hard this hour that have not already run away."""
    out = []
    for r in rows:
        hour = r.get("hour_pct")
        if hour is None or float(hour) < CRYPTO_VELOCITY_1H:
            continue
        if r["pct"] > CRYPTO_VELOCITY_MAX_24H:
            continue          # the move already happened; this is the tail
        key = f"cryptovel:{r['symbol']}"
        if now_ts - fired.get(key, 0) < VELOCITY_COOLDOWN_SEC:
            continue
        out.append((key, r))
    out.sort(key=lambda t: -float(t[1]["hour_pct"]))
    return out


def collect_relvol(rows, fired, now_ts, fraction):
    """Stocks trading far above their normal pace, and rising."""
    out = []
    for r in rows:
        avg = r.get("avg_volume") or 0
        if avg <= 0 or r["pct"] < RELVOL_MIN_GAIN:
            continue
        projected = r["volume"] / fraction
        ratio = projected / avg
        if ratio < RELVOL_MIN:
            continue
        key = f"relvol:{r['symbol']}"
        if now_ts - fired.get(key, 0) < VELOCITY_COOLDOWN_SEC:
            continue
        out.append((key, r, ratio))
    out.sort(key=lambda t: -t[2])
    return out


def send_velocity(pending, fired, now_ts, overnight):
    if overnight:
        # An early signal is by definition a small move so far - never worth
        # a 3am wake-up, and not recorded, so it can fire again in daylight.
        if pending:
            log(f"  {len(pending)} velocity signals held until morning")
        return
    for key, r in pending[:MAX_ALERTS_PER_RUN]:
        fired[key] = now_ts
        push(f"{r['symbol']} running {float(r['hour_pct']):+.1f}%/hr",
             format_body(r),
             priority="high",
             tags="rocket",
             click=robinhood_url(r))
        log(f"  VELOCITY {r['symbol']} 1h {float(r['hour_pct']):+.1f}% "
            f"(24h {r['pct']:+.1f}%)")


def send_relvol(pending, fired, now_ts, overnight):
    for key, r, ratio in pending[:MAX_ALERTS_PER_RUN]:
        fired[key] = now_ts
        body = (f"{r['name']}\n"
                f"now +{r['pct']:.1f}%  -  ${r['price']:,.2f}\n"
                f"{ratio:.1f}x normal volume for this time of day")
        push(f"{r['symbol']} unusual volume {ratio:.1f}x",
             body,
             priority="high",
             tags="eyes",
             click=robinhood_url(r))
        log(f"  RELVOL {r['symbol']} {ratio:.1f}x on +{r['pct']:.1f}%")


def collect_pending(rows, levels, fired, prefix):
    pending = []
    for r in rows:
        crossed = [lv for lv in levels if r["pct"] >= lv]
        if not crossed:
            continue
        highest = max(crossed)
        key = f"{prefix}:{r['symbol']}"
        if highest <= fired.get(key, 0.0):
            continue
        pending.append((highest, key, r))
    pending.sort(key=lambda t: -t[2]["pct"])
    return pending


def send_alerts(pending, fired, high_level, overnight):
    sent, held = 0, 0
    for level, key, r in pending[:MAX_ALERTS_PER_RUN]:
        if overnight and level < high_level:
            # Held, deliberately not recorded in `fired`, so the morning runs
            # pick it up again if it is still running.
            held += 1
            continue
        fired[key] = level
        sent += 1
        priority = priority_for(level, high_level, overnight)
        push(f"{r['symbol']} crossed +{level:.0f}%",
             format_body(r),
             priority=priority,
             click=robinhood_url(r))
        log(f"  ALERT {r['symbol']} +{r['pct']:.1f}% "
            f"(crossed {level:.0f}%, {priority})")

    if held:
        log(f"  {held} held until morning (below the overnight bar)")

    extra = len(pending) - MAX_ALERTS_PER_RUN
    if extra > 0 and sent:
        push("More movers crossing",
             f"{extra} more crossed a level. They'll follow shortly.",
             priority="default",
             tags="fire")
        log(f"  {extra} queued for the next run")


# ---------------------------------------------------------------------------

def main():
    # The test push comes first so it works at any hour, market open or not.
    if os.environ.get("TEST_PUSH") == "1":
        push("Market Movers test",
             "Phone alerts are wired up correctly.\nTap to open Robinhood.",
             priority="default", tags="white_check_mark",
             click="https://robinhood.com/stocks/")
        log("Test notification sent")
        return 0

    force = os.environ.get("FORCE_RUN") == "1"
    overnight = in_overnight()
    now_ts = datetime.now(timezone.utc).timestamp()
    state = load_state()
    fired = state["fired"]
    tradable = state["tradable"]

    # --- Crypto: always, it never closes ---
    crypto_rows = screen_crypto()
    log(f"{len(crypto_rows)} coins pass the crypto filters")

    # Velocity first: this is the early signal, so it goes out ahead of the
    # magnitude alerts if the run cap forces a choice.
    vel = filter_tradable(collect_velocity(crypto_rows, fired, now_ts),
                          tradable, 1)
    log(f"{len(vel)} tradable coins moving hard this hour"
        f"{' (overnight)' if overnight else ''}")
    send_velocity(vel, fired, now_ts, overnight)

    crypto_pending = filter_tradable(
        collect_pending(crypto_rows, CRYPTO_ALERT_LEVELS, fired, "crypto"),
        tradable, 2)
    log(f"{len(crypto_pending)} new tradable crypto threshold crossings")
    send_alerts(crypto_pending, fired, CRYPTO_HIGH_PRIORITY_LEVEL, overnight)

    # --- Stocks: market hours only ---
    if force or market_is_open():
        try:
            import yfinance as yf
        except ImportError:
            log("yfinance is not installed")
            save_state(state)
            return 2

        # Screen down to the relative-volume floor, which is lower than the
        # magnitude floor - a stock only up 3% can still be the one running.
        floor = min(MIN_GAIN_PCT, RELVOL_MIN_GAIN)
        stock_rows = screen_stocks(yf, floor)
        with_avg = sum(1 for r in stock_rows if (r.get("avg_volume") or 0) > 0)
        log(f"{len(stock_rows)} stocks pass the screen "
            f"({with_avg} with average-volume data)")

        fraction = session_fraction()
        if fraction is None:
            log("  outside the regular session - skipping relative volume")
        else:
            rv = filter_tradable(
                collect_relvol(stock_rows, fired, now_ts, fraction),
                tradable, 1)
            log(f"{len(rv)} tradable stocks above {RELVOL_MIN:.0f}x normal "
                f"({fraction * 100:.0f}% of the session elapsed)")
            send_relvol(rv, fired, now_ts, overnight)

        movers = [r for r in stock_rows if r["pct"] >= MIN_GAIN_PCT]
        stock_pending = filter_tradable(
            collect_pending(movers, ALERT_LEVELS, fired, "stock"), tradable, 2)
        log(f"{len(stock_pending)} new tradable stock threshold crossings")
        send_alerts(stock_pending, fired, HIGH_PRIORITY_LEVEL, overnight)
    else:
        log("Outside market hours - skipping the stock screen")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
