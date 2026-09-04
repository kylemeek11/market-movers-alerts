#!/usr/bin/env python3
"""
Market Movers phone alerts.

Runs on GitHub Actions, independent of any Mac. Screens the US stock market
and the crypto market for names up past a set of thresholds, and pushes a
notification to your phone via ntfy when one crosses a level it hasn't
crossed today. Each alert links straight to that symbol on Robinhood.

Stocks are screened during market hours only; crypto trades around the
clock, so it is screened on every run, with overnight alerts sent silently.

Deliberately light: stocks need only the screener's quote payload and crypto
is one CoinGecko call, so a run finishes in seconds.
"""

from __future__ import annotations

import json
import os
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

# --- Shared -----------------------------------------------------------------
MAX_ALERTS_PER_RUN = 6
HIGH_PRIORITY_LEVEL = 20.0             # stocks at/above this break through silence
CRYPTO_HIGH_PRIORITY_LEVEL = 40.0      # crypto has to move harder to do the same

MARKET_TZ = "America/New_York"
MARKET_OPEN_HOUR = 8
MARKET_CLOSE_HOUR = 17

# Overnight crypto alerts are sent at low priority: they arrive silently and
# are waiting in the morning rather than waking you up.
USER_TZ = "America/Chicago"
QUIET_START_HOUR = 22
QUIET_END_HOUR = 7

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


def in_quiet_hours():
    now = local_now(USER_TZ)
    if now is None:
        return False         # if the lookup fails, prefer a real alert
    return now.hour >= QUIET_START_HOUR or now.hour < QUIET_END_HOUR


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

def screen_stocks(yf):
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
        if chg is None or chg < MIN_GAIN_PCT:
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
        if chg is None or float(chg) < min(CRYPTO_ALERT_LEVELS):
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
            "hour_pct": c.get("price_change_percentage_1h_in_currency"),
        })
    out.sort(key=lambda r: -r["pct"])
    return out


# --- State ------------------------------------------------------------------

def load_state():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        state = json.loads(STATE_FILE.read_text())
        if state.get("date") == today:
            return state
        log("  state is from a previous day - starting fresh")
    except (OSError, ValueError):
        log("  no previous state found")
    return {"date": today, "fired": {}}


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


def send_alerts(pending, fired, high_level, quiet):
    for level, key, r in pending[:MAX_ALERTS_PER_RUN]:
        fired[key] = level
        if quiet:
            priority = "low"
        elif level >= high_level:
            priority = "high"
        else:
            priority = "default"
        push(f"{r['symbol']} crossed +{level:.0f}%",
             format_body(r),
             priority=priority,
             click=robinhood_url(r))
        log(f"  ALERT {r['symbol']} +{r['pct']:.1f}% "
            f"(crossed {level:.0f}%, {priority})")

    extra = len(pending) - MAX_ALERTS_PER_RUN
    if extra > 0:
        push("More movers crossing",
             f"{extra} more crossed a level. They'll follow shortly.",
             priority="low" if quiet else "default",
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
    quiet = in_quiet_hours()
    state = load_state()
    fired = state["fired"]

    # --- Crypto: always, it never closes ---
    crypto_rows = screen_crypto()
    log(f"{len(crypto_rows)} coins pass the crypto filters")
    crypto_pending = collect_pending(crypto_rows, CRYPTO_ALERT_LEVELS,
                                     fired, "crypto")
    log(f"{len(crypto_pending)} new crypto threshold crossings"
        f"{' (quiet hours)' if quiet else ''}")
    send_alerts(crypto_pending, fired, CRYPTO_HIGH_PRIORITY_LEVEL, quiet)

    # --- Stocks: market hours only ---
    if force or market_is_open():
        try:
            import yfinance as yf
        except ImportError:
            log("yfinance is not installed")
            save_state(state)
            return 2
        stock_rows = screen_stocks(yf)
        log(f"{len(stock_rows)} stocks pass the filters")
        stock_pending = collect_pending(stock_rows, ALERT_LEVELS,
                                        fired, "stock")
        log(f"{len(stock_pending)} new stock threshold crossings")
        send_alerts(stock_pending, fired, HIGH_PRIORITY_LEVEL, quiet)
    else:
        log("Outside market hours - skipping the stock screen")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
