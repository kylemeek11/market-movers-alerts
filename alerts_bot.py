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

ALERT_LEVELS = [15.0, 20.0, 30.0, 50.0]

# ADRs and other foreign-domiciled listings. Yahoo does not label these
# directly, but it does report the currency the company keeps its books in:
# 17 Education comes back CNY, SoftBank JPY, Wing Yip KRW, while US operators
# come back USD. A missing value is treated as domestic - leveraged ETFs omit
# the field entirely and are not ADRs.
EXCLUDE_FOREIGN = True

# --- Crypto settings --------------------------------------------------------
# Crypto is far more volatile than equities, so the thresholds start higher:
# a 10% day is unremarkable for a coin and would just be noise.
CRYPTO_ALERT_LEVELS = [15.0, 25.0, 40.0, 60.0, 100.0]
CRYPTO_MIN_24H_VOLUME = 10_000_000     # USD traded in 24h
CRYPTO_MIN_MARKET_CAP = 50_000_000
CRYPTO_TOP_N = 250                     # how many coins by market cap to watch

# Tracked but never interesting: a stablecoin does not "run", and its normal
# few-tenths wobble is pure noise in a rate window. A depeg is real news but
# not something to ride on Robinhood, so it stays out of the universe.
STABLE_SKIP = {
    "USDT", "USDC", "DAI", "USDE", "FDUSD", "USDS", "PYUSD", "TUSD", "USD1",
    "BUIDL", "RLUSD", "USDF", "USDY", "LUSD", "FRAX", "USDD", "GUSD", "USDP",
    "XAUT", "PAXG",
}

# Ticker sanity. CoinGecko lists coins whose "symbol" is Chinese characters
# (龙虾, 牛来 and friends). Robinhood tickers are plain ASCII, so these can
# never be tradable - and worse, they used to slip through: the URL could not
# be built, the check raised UnicodeEncodeError, and the fail-open rule for
# network blips let them alert. The notification was unreadable anyway,
# because non-ASCII is stripped from ntfy headers, so both title and link
# came out as "--".
TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,14}$")

# Relative volume: unusual participation. Kept for stocks only, and with a
# caveat - on crypto, requiring a volume surge alongside the price move made
# the alert 85 minutes LATER for six points of precision. Confirmation costs
# timeliness, which is the whole thing we are buying here.

# Rate of change - the signal. Every run stamps the price of every name in the
# TRACKED UNIVERSE, which is deliberately much wider than the set allowed to
# alert: a coin sitting flat when its run begins has to already be stamped, or
# there is nothing to measure the run against. A later run compares against
# its own earlier stamp, and elapsed time is read off the stamps rather than
# assumed, because GitHub's scheduler drifts.
#
# The window and the bar are measured, not guessed. Backtest over 81
# Robinhood-tradable coins, 5-minute candles, 2026-09-13 to 09-20: 151 runs of
# +8% or more, median run +10.9% lasting about twelve hours. Runs are SLOW.
# Measured from the bottom of each one, a run is up 2% about 75 minutes in
# with 8.3% still to come, up 3% at 130 minutes with 7.1% to come, up 5% at
# 255 minutes with 5.2% to come. You are not late at 2%.
#
# What did NOT work, tested and discarded:
#   - Short windows. Every rule on a 10-20 minute lookback at 4% or more had
#     no follow-through at all. A sharp spike is the END of a move.
#   - Volume confirmation and breakout filters. Both bought a few points of
#     precision at the cost of 85-190 minutes of lateness.
# A 50-75 minute lookback at a low bar beat everything else tried.
#
# Measured at 2%: catches 89% of runs, lands a median 3.25 hours into a
# 12-hour run with 6.6% still to come, and about half of all alerts land
# inside a real +8% run against a 1.7% base rate.
#
# SET TO 5% ON 2026-09-22 at Kyle's request. The 2% bar produced ~400
# alerts/day in live use - far above the ~82/day the backtest projected,
# because the backtest week was calmer than the day it shipped into and
# because the stock side was never backtested at all. Measured against his
# own 24 hours of alerts, 5% leaves about 41/day. The cost is real and worth
# restating before anyone lowers it again: at 5% the signal catches roughly a
# quarter of runs rather than nine in ten, and arrives much later. On the
# NEAR run it fires at $3.91 instead of $3.79 - still well ahead of the $4.23
# the day threshold managed, but 19 minutes behind. 3.0 is the middle
# (~126/day on that same sample). See SETUP.md for the full table.
#
# Honest limit: Yahoo delays stock quotes ~15 minutes, so a stock rate is a
# real move that finished ~15 minutes ago. CoinGecko caches about a minute,
# so crypto rates are near-live. The stock bar is NOT backed by the backtest
# above, which was crypto only.
RATE_WINDOW_MIN = 50.0          # ignore a stamp younger than this
RATE_WINDOW_MAX_MIN = 75.0      # ...or older than this
CRYPTO_RATE_PCT = 3.0           # crypto: gain across that window worth a look
STOCK_RATE_PCT = 3.0            # stocks: same bar

# Volume confirmation. Scored against 164 of Kyle's own alerts from
# 2026-09-22 (5-minute bars pulled for each symbol, outcome measured over the
# 4 hours after each buzz), relative volume separates the runs from the
# dead ends about as well as anything tested:
#
#   relvol at alert   goes +3% after   dead end (<1%)
#   under 2x               16%              48%
#   2-3x                   22%              37%
#   3-5x                   35%              41%
#   5-10x                  31%              23%
#
# Paired with a 3% bar that lands at ~37 alerts/day with a 33% hit rate and
# 27% dead ends - versus the 5%-alone setting it replaces, which was ~39/day
# at 25% and 31%. Same notification count, better hits, and it fires at 3%
# rather than 5% so the entry is earlier.
#
# An earlier week-long crypto backtest said volume confirmation was not worth
# it. That test asked whether an alert sat inside a run; this one asks whether
# the price actually went up afterwards, which is the question that matters.
# Lowered 5.0 -> 2.5 on 2026-09-22. At 5x the price bar was being cleared long
# before the volume caught up, so the alert arrived after the move: UNI was up
# 4.9% at 17:15 CT but did not buzz until 17:56 at 9.4%, and ZEC likewise at
# 7.2%. Replayed against those two, 2.5x fires UNI at 17:25 up 4.0% ($9.75 vs
# the $10.28 it actually alerted at) and ZEC at 18:10 up 7.4%.
#
# The cost is known and accepted: in the scoring sample the 3-5x band had the
# best hit rate of any bucket (35% went on to gain 3%+), and 2-3x fell to 22%.
# Sitting at 2.5 trades some of that accuracy for about 30 minutes of warning.
#
# Note the crypto figure is derived from how fast CoinGecko's 24h volume moves
# and reads a little high - the bot called both of those alerts 6x where
# Coinbase's own volume put them at 5.4x and 3.5x - so the effective bar is
# somewhat looser than the number suggests.
RATE_MIN_RELVOL = 2.5           # window volume vs the name's own normal pace
RATE_COOLDOWN_SEC = 45 * 60     # re-alert the same name at most this often
RATE_MARKS_KEPT = 24            # enough stamps to span the window with drift

# Crypto is highly correlated, so a market-wide lift can light up the whole
# board at once. Measured over the backtest week, the share of the universe
# clearing the 2% bar in one cycle is 3% at the median and 9% at the 90th
# percentile, so this only trips in a genuine melt-up (0.4% of cycles). Below
# it the per-run cap does the work instead, sending the biggest movers rather
# than nothing - a broad rally is still rideable. The day thresholds fire
# either way, so nothing genuinely big goes unreported.
RATE_BREADTH_MAX = 0.50

RELVOL_MIN = 3.0                       # projected volume vs normal
RELVOL_MIN_GAIN = 3.0                  # and it has to actually be rising
RELVOL_COOLDOWN_SEC = 4 * 3600         # re-alert the same name at most this often

# The stock screen is the tracking universe, so its floor has to sit below
# every alert floor - same reason as the crypto split above. A stock only up
# 0.5% now is exactly the one whose run we want stamped from the start.
STOCK_TRACK_FLOOR = 0.5
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

# Robinhood publishes the list itself. This is strictly better than inferring
# tradability one page at a time: one request instead of dozens, it answers
# the question directly, and because the universe can be filtered up front, a
# coin that is not on Robinhood never gets stamped, never climbs the rate
# track, and never vanishes silently between the signal and the push. Crypto
# only - stocks still use the per-symbol page check below.
ROBINHOOD_PAIRS_URL = "https://nummus.robinhood.com/currency_pairs/"
ROBINHOOD_MIN_PAIRS = 20         # fewer than this means the payload is wrong
ROBINHOOD_SCAN_BYTES = 600_000   # the field sits ~200KB in; no need for the rest
TRADABILITY_RE = re.compile(rb'"tradability"\s*:\s*"([a-z_]+)"', re.I)
# Bump when the tradability logic changes, so answers cached by an older and
# possibly wrong version of the gate are thrown away instead of being trusted.
GATE_VERSION = 2

# --- Shared -----------------------------------------------------------------
# At the 2% bar a cycle sends 2.5 alerts on average and 6 at the 90th
# percentile, so a cap of 6 would clip 7% of cycles; 10 clips 3%. Anything
# over the cap is not lost, it just waits for the next run.
MAX_ALERTS_PER_RUN = 10
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

def is_foreign(q):
    """ADR / foreign-domiciled listing, by reporting currency then by name."""
    currency = (q.get("financialCurrency") or "").strip().upper()
    if currency and currency != "USD":
        return True
    name = f"{q.get('shortName') or ''} {q.get('longName') or ''}".upper()
    return "AMERICAN DEPOSITARY" in name or " ADR" in name or name.endswith("ADR")


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
            # This term also keeps ETFs out for free: Yahoo models funds with
            # netAssets rather than a market cap, so requiring one excludes
            # them at the source. Verified against a live run - 120 survivors,
            # zero carrying any fund field. The local marketCap check below
            # catches them again on the fallback path. No ETF filter needed.
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

    out, foreign = [], 0
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
        if EXCLUDE_FOREIGN and is_foreign(q):
            foreign += 1
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
    if foreign:
        log(f"  {foreign} ADR/foreign listings excluded")
    out.sort(key=lambda r: -r["pct"])
    return out


def robinhood_symbols():
    """Every coin Robinhood will actually let you trade, or None.

    None means "could not find out", and the caller falls back to the
    per-symbol page check rather than silently narrowing the universe to
    nothing - the failure mode that would quietly stop all crypto alerts.
    """
    import urllib.request

    req = urllib.request.Request(
        ROBINHOOD_PAIRS_URL,
        headers={"Accept": "application/json",
                 "User-Agent": "market-movers-alerts/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=ROBINHOOD_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"  Robinhood pair list unavailable ({exc}) - using page checks")
        return None

    out = set()
    for pair in (data.get("results") or []):
        if pair.get("tradability") != "tradable":
            continue
        code = ((pair.get("asset_currency") or {}).get("code") or "").upper()
        if code:
            out.add(code)
    if len(out) < ROBINHOOD_MIN_PAIRS:
        log(f"  Robinhood pair list looked wrong ({len(out)}) - using page checks")
        return None
    return out


def screen_crypto(allowed=None):
    """Top coins by market cap, filtered to what is liquid and buyable."""
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

    out, odd, off = [], 0, 0
    for c in coins:
        sym = (c.get("symbol") or "").upper()
        if not sym:
            continue
        if not TICKER_RE.match(sym):
            odd += 1
            continue
        chg = c.get("price_change_percentage_24h_in_currency")
        if chg is None:
            chg = c.get("price_change_percentage_24h")
        price = c.get("current_price") or 0
        vol = c.get("total_volume") or 0
        cap = c.get("market_cap") or 0
        if chg is None:
            continue
        if vol < CRYPTO_MIN_24H_VOLUME or cap < CRYPTO_MIN_MARKET_CAP:
            continue
        if sym in STABLE_SKIP:
            continue
        if allowed is not None and sym not in allowed:
            off += 1
            continue
        hour = c.get("price_change_percentage_1h_in_currency")
        # Everything liquid is TRACKED; only some of it is a CANDIDATE for the
        # day-threshold alerts. Splitting these is the whole fix:
        # the old code dropped a coin right here unless it had already moved,
        # so the rate tracks never held a stamp from before a run started and
        # could only ever confirm what the day thresholds were about to say.
        #
        # CoinGecko's 1h field is NOT a trailing 60 minutes - on 2026-09-20 it
        # read +5.3% for NEAR when the real 1h move was +13.5%. The signal
        # that used to depend on it is gone; our own stamps answer the same
        # question correctly. It is carried here only for the log.
        out.append({
            "kind": "crypto",
            "symbol": sym,
            "name": c.get("name") or sym,
            "pct": float(chg),
            "price": float(price),
            "dollars": float(vol),
            "hour_pct": hour,
            "candidate": float(chg) >= min(CRYPTO_ALERT_LEVELS),
        })
    if odd:
        log(f"  {odd} coins skipped for non-ticker symbols")
    if off:
        log(f"  {off} liquid coins skipped - not tradable on Robinhood")
    out.sort(key=lambda r: -r["pct"])
    return out


# --- State ------------------------------------------------------------------

def load_state():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        state = json.loads(STATE_FILE.read_text())
        if state.get("date") == today:
            state.setdefault("tradable", {})
            state.setdefault("marks", {})
            state.setdefault("rh_symbols", None)
            if state.get("gate") != GATE_VERSION:
                log("  tradability cache came from an older gate - clearing")
                state["tradable"] = {}
                state["gate"] = GATE_VERSION
            return state
        log("  state is from a previous day - starting fresh")
    except (OSError, ValueError):
        log("  no previous state found")
    return {"date": today, "fired": {}, "tradable": {}, "marks": {},
            "rh_symbols": None, "gate": GATE_VERSION}


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except OSError as exc:
        log(f"  could not save state: {exc}")


# --- Alerting ---------------------------------------------------------------

def robinhood_url(row):
    # quote() so a symbol with odd characters produces a valid request rather
    # than raising deep inside http.client.
    from urllib.parse import quote
    sym = quote(str(row["symbol"]), safe="")
    kind = "crypto" if row["kind"] == "crypto" else "stocks"
    return f"https://robinhood.com/{kind}/{sym}"


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

    # A symbol that is not a plain ticker cannot be a Robinhood listing. This
    # fails CLOSED on purpose: it is a fact about the symbol, not a transient
    # network problem, so the fail-open rule below must not apply to it.
    if not TICKER_RE.match(str(row["symbol"])):
        cache[key] = False
        log(f"  skip {row['symbol']} - not a Robinhood-style ticker")
        return False

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
    # Deliberately no 1h figure: the only source for it disagrees with the
    # actual hourly move by enough to mislead (see screen_crypto).
    line2 = "  -  ".join([f"now +{row['pct']:.1f}%", price_str])
    if row["kind"] == "crypto":
        line3 = f"${row['dollars'] / 1e6:,.0f}M 24h volume"
    else:
        line3 = f"${row['dollars'] / 1e6:,.0f}M traded"
    return f"{row['name']}\n{line2}\n{line3}"


def volume_counter(row):
    """The running volume figure this row carries, or None.

    Neither source gives volume for an arbitrary recent window, so we stamp a
    counter and difference it. Crypto gets CoinGecko's rolling 24h dollar
    volume; stocks get today's cumulative share volume from the screener.
    """
    if row["kind"] == "crypto":
        return row.get("dollars")
    return row.get("volume")


def record_marks(rows, marks, now_ts):
    """Stamp price and a volume counter for every name in the universe."""
    for r in rows:
        key = f"{r['kind']}:{r['symbol']}"
        hist = marks.setdefault(key, [])
        hist.append([now_ts, r["price"], volume_counter(r)])
        # Drop anything too old to be useful, then cap the list.
        cutoff = now_ts - RATE_WINDOW_MAX_MIN * 60
        hist[:] = [m for m in hist if m[0] >= cutoff][-RATE_MARKS_KEPT:]


def drop_stale_marks(marks, now_ts):
    """Forget names that have left the universe, so the state stops growing."""
    cutoff = now_ts - RATE_WINDOW_MAX_MIN * 60
    for key in [k for k, h in marks.items() if not h or h[-1][0] < cutoff]:
        del marks[key]


def oldest_mark(hist, now_ts, lo_min, hi_min):
    """The oldest stamp whose age falls inside [lo_min, hi_min].

    Stamps written before volume tracking have two fields instead of three,
    so read by index rather than unpacking - otherwise every name goes dark
    for a window's length after a deploy.
    """
    best = None
    for m in hist:
        ts, px = m[0], m[1]
        vol = m[2] if len(m) > 2 else None
        elapsed_min = (now_ts - ts) / 60.0
        if elapsed_min < lo_min or elapsed_min > hi_min:
            continue
        if px and px > 0 and (best is None or ts < best[0]):
            best = (ts, px, elapsed_min, vol)
    return best


def move_since(price, mark):
    return (price - mark[1]) / mark[1] * 100.0


def relative_volume(row, mark):
    """How busy the last ~hour was, against this name's own normal pace.

    Returns None when it cannot be worked out, which the caller treats as a
    reason not to alert - but logs, so a data problem never silently mutes
    everything.

    Crypto: the counter is a ROLLING 24h total, so its change over a window
    is (traded in the window) minus (what dropped off the back). The latter
    is a normal window's worth, so traded ~= delta + V24*w/1440 and the ratio
    against normal reduces to 1 + (delta/V24)*(1440/w).

    Stocks: the counter is today's cumulative volume, so the delta IS the
    window's volume. Normal is the 3-month daily average pro-rated to the
    window. A negative delta means the window crossed a session boundary and
    the counter reset; there is nothing to measure, so return None.
    """
    prior = mark[3] if len(mark) > 3 else None
    now_vol = volume_counter(row)
    minutes = mark[2]
    if prior is None or now_vol is None or minutes <= 0:
        return None
    delta = now_vol - prior

    if row["kind"] == "crypto":
        if prior <= 0:
            return None
        return 1.0 + (delta / prior) * (1440.0 / minutes)

    avg = row.get("avg_volume") or 0
    if avg <= 0 or delta < 0:
        return None
    normal = avg * (minutes / SESSION_MINUTES)
    if normal <= 0:
        return None
    return delta / normal


def collect_rate(rows, marks, fired, now_ts, pct):
    """Names climbing steadily, on volume, enough to be worth a look.

    Compares against the OLDEST stamp still inside the window, so a delayed
    run widens the comparison rather than breaking it, and the elapsed
    minutes actually used are carried through to the alert text. Sorted by
    size of move, because that is the order the per-run cap should keep.

    Returns (alerts, quiet, unknown): names that cleared both bars, names
    that climbed but on ordinary volume, and names whose volume could not be
    worked out. The last two are counted in the log so that a data problem
    shows up as a number rather than as silence.
    """
    out, quiet, unknown = [], 0, 0
    for r in rows:
        key = f"{r['kind']}:{r['symbol']}"
        hist = marks.get(key) or []
        mark = oldest_mark(hist, now_ts, RATE_WINDOW_MIN, RATE_WINDOW_MAX_MIN)
        if mark is None:
            continue
        move = move_since(r["price"], mark)
        if move < pct:
            continue
        rv = relative_volume(r, mark)
        if rv is None:
            unknown += 1
            continue
        if rv < RATE_MIN_RELVOL:
            quiet += 1
            continue
        fkey = f"rate:{key}"
        if now_ts - fired.get(fkey, 0) < RATE_COOLDOWN_SEC:
            continue
        out.append((fkey, r, move, mark[2], rv))
    out.sort(key=lambda t: -t[2])
    return out, quiet, unknown


def rate_bars(rows, marks, now_ts):
    """Shadow counts at other thresholds, for tuning without a missed run.

    Ignores cooldown and tradability on purpose: this is the raw shape of the
    market this minute, not a count of alerts that would have gone out. The
    only honest way to move the bars is to watch these for a few days.
    """
    bars = (1.5, 2.0, 2.5, 3.0, 4.0, 6.0)
    vbars = (2.0, 3.0, 5.0, 8.0)
    counts = [0] * len(bars)
    vcounts = [0] * len(vbars)
    ready = no_vol = 0
    for r in rows:
        hist = marks.get(f"{r['kind']}:{r['symbol']}") or []
        mark = oldest_mark(hist, now_ts, RATE_WINDOW_MIN, RATE_WINDOW_MAX_MIN)
        if mark is None:
            continue
        ready += 1
        mv = move_since(r["price"], mark)
        for i, bar in enumerate(bars):
            if mv >= bar:
                counts[i] += 1
        rv = relative_volume(r, mark)
        if rv is None:
            no_vol += 1
            continue
        for i, bar in enumerate(vbars):
            if rv >= bar:
                vcounts[i] += 1
    return ("  rate bars (" + str(ready) + " with history)  "
            + "/".join(f"{b:g}%:{n}" for b, n in zip(bars, counts))
            + "   volume "
            + "/".join(f"{b:g}x:{n}" for b, n in zip(vbars, vcounts))
            + (f"   ({no_vol} no volume figure)" if no_vol else ""))


def too_broad(pending, rows):
    """True when so much of the universe is climbing that it is just beta."""
    if not pending or not rows:
        return False
    return len(pending) > max(3, int(RATE_BREADTH_MAX * len(rows)))


def send_rate(pending, fired, now_ts, overnight, high_bar):
    held = 0
    for fkey, r, move, elapsed_min, rv in pending[:MAX_ALERTS_PER_RUN]:
        # Overnight this is an early signal on a small move - hold it, and do
        # not record it, so it can fire again in daylight if still running.
        if overnight and move < high_bar:
            held += 1
            continue
        fired[fkey] = now_ts
        price_line = (f"  -  ${r['price']:,.2f}" if r["price"] >= 1 else "")
        push(f"{r['symbol']} +{move:.1f}% in {elapsed_min:.0f} min",
             f"{r['name']}\n"
             f"+{move:.1f}% in {elapsed_min:.0f} min  -  {rv:.0f}x volume\n"
             f"now +{r['pct']:.1f}% on the day{price_line}",
             priority="max" if overnight else "high",
             tags="zap",
             click=robinhood_url(r))
        log(f"  RATE {r['symbol']} +{move:.1f}% over {elapsed_min:.0f}min "
            f"on {rv:.1f}x volume (day {r['pct']:+.1f}%)")
    if held:
        log(f"  {held} rate signals held until morning")


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
        if now_ts - fired.get(key, 0) < RELVOL_COOLDOWN_SEC:
            continue
        out.append((key, r, ratio))
    out.sort(key=lambda t: -t[2])
    return out


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
    marks = state["marks"]

    # --- Crypto: always, it never closes ---
    allowed = state.get("rh_symbols")
    if allowed is None:
        found = robinhood_symbols()
        if found is not None:
            allowed = sorted(found)
            state["rh_symbols"] = allowed
            log(f"  Robinhood lists {len(allowed)} tradable coins")
    crypto_rows = screen_crypto(set(allowed) if allowed else None)
    crypto_movers = [r for r in crypto_rows if r.get("candidate")]
    log(f"tracking {len(crypto_rows)} liquid coins, "
        f"{len(crypto_movers)} in alert range")
    record_marks(crypto_rows, marks, now_ts)

    # Rate of change first: it is the earliest signal, so it wins the run cap.
    craw, cquiet, cunknown = collect_rate(crypto_rows, marks, fired, now_ts,
                                          CRYPTO_RATE_PCT)
    if cquiet or cunknown:
        log(f"  {cquiet} coins climbing on ordinary volume"
            + (f", {cunknown} with no volume figure" if cunknown else ""))
    if too_broad(craw, crypto_rows):
        log(f"  {len(craw)} of {len(crypto_rows)} coins climbing - "
            f"market-wide, holding rate alerts")
        craw = []
    crate = filter_tradable(craw, tradable, 1)
    if craw:
        dropped = len(craw) - len(crate)
        log(f"{len(crate)} coins climbing fast enough to flag"
            + (f" ({dropped} dropped - not on Robinhood)" if dropped else ""))
    send_rate(crate, fired, now_ts, overnight, CRYPTO_HIGH_PRIORITY_LEVEL)
    log(rate_bars(crypto_rows, marks, now_ts))

    crypto_pending = filter_tradable(
        collect_pending(crypto_movers, CRYPTO_ALERT_LEVELS, fired, "crypto"),
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

        # Screen well below every alert floor: this list is the tracking
        # universe, and a stock only up 0.5% now is exactly the one whose run
        # we want already stamped when it starts.
        stock_rows = screen_stocks(yf, STOCK_TRACK_FLOOR)
        with_avg = sum(1 for r in stock_rows if (r.get("avg_volume") or 0) > 0)
        log(f"tracking {len(stock_rows)} stocks "
            f"({with_avg} with average-volume data)")

        record_marks(stock_rows, marks, now_ts)
        sraw, squiet, sunknown = collect_rate(stock_rows, marks, fired,
                                              now_ts, STOCK_RATE_PCT)
        if squiet or sunknown:
            log(f"  {squiet} stocks climbing on ordinary volume"
                + (f", {sunknown} with no volume figure" if sunknown else ""))
        if too_broad(sraw, stock_rows):
            log(f"  {len(sraw)} of {len(stock_rows)} stocks climbing - "
                f"market-wide, holding rate alerts")
            sraw = []
        srate = filter_tradable(sraw, tradable, 1)
        if sraw:
            dropped = len(sraw) - len(srate)
            log(f"{len(srate)} stocks climbing fast enough to flag"
                + (f" ({dropped} dropped - not on Robinhood)" if dropped else ""))
        send_rate(srate, fired, now_ts, overnight, HIGH_PRIORITY_LEVEL)
        log(rate_bars(stock_rows, marks, now_ts))

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

    # Makes a silent failure loud: if the state cache ever stopped carrying
    # marks across runs, this would read 0 and no rate alert could ever fire.
    # The slow-window count is the one that matters now - it is what the climb
    # track needs, and it is the last thing to recover after a cache miss.
    drop_stale_marks(marks, now_ts)
    with_prior = sum(1 for h in marks.values() if len(h) >= 2)
    slow_ready = sum(1 for h in marks.values()
                     if oldest_mark(h, now_ts, RATE_WINDOW_MIN,
                                    RATE_WINDOW_MAX_MIN) is not None)
    log(f"tracking {len(marks)} names, {with_prior} with a prior stamp, "
        f"{slow_ready} with {RATE_WINDOW_MIN:.0f}min+ of history")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
