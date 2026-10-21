#!/usr/bin/env python3
"""Regression tests for the alert logic. Run: python3 test_alerts_bot.py

No pytest, no network. The centrepiece is a replay of the run NEAR made on
2026-09-20 - the one the bot missed - against the real 5-minute price series
and the real GitHub Actions run timestamps from that morning.
"""

import io
import json
import sys
import urllib.request
from datetime import datetime, timezone

import alerts_bot as ab

# These suites test the price signal and the CoinGecko arithmetic; keep the
# Coinbase confirmation out of the way so nothing here touches the network.
# The real function is kept for its own section near the end.
REAL_COINBASE_RELVOL = ab.coinbase_relvol
ab.coinbase_relvol = lambda *a, **k: None

# A book with room for a buy, so the format checks below see the loud path;
# the gate itself is tested in "Quieter alerts" near the end.
OPEN_BOOK = {"positions": [], "_lot_size_usd": 500, "_buys_allowed": True}

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def ct(hhmm):
    """2026-09-20 CT (UTC-5) -> epoch seconds."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 9, 20, h + 5, m, tzinfo=timezone.utc).timestamp()


# NEAR, 2026-09-20, CoinGecko 5-minute series (verified against the live API).
NEAR_SERIES = [
    ("09:30", 3.6796), ("09:35", 3.6862), ("09:40", 3.6979), ("09:45", 3.6900),
    ("09:50", 3.6827), ("09:55", 3.6977), ("10:00", 3.6772), ("10:05", 3.6704),
    ("10:10", 3.6334), ("10:15", 3.6551), ("10:20", 3.7051), ("10:25", 3.7435),
    ("10:30", 3.7176), ("10:35", 3.7231), ("10:40", 3.7387), ("10:45", 3.7941),
    ("10:50", 3.7955), ("10:55", 3.8221), ("11:00", 3.9068), ("11:05", 3.9755),
    ("11:10", 3.9580), ("11:15", 3.9740), ("11:20", 4.0392), ("11:25", 4.0567),
    ("11:30", 4.1908), ("11:35", 4.2256), ("11:40", 4.2463), ("11:45", 4.2779),
]

# Actual workflow run start times that morning (runs 2567-2583).
RUN_TIMES = ["09:31", "09:40", "09:46", "09:52", "09:58", "10:06", "10:18",
             "10:28", "10:36", "10:43", "10:50", "10:56", "11:02", "11:16",
             "11:27", "11:36", "11:44"]


def near_price_at(hhmm):
    target = ct(hhmm)
    return min(NEAR_SERIES, key=lambda r: abs(ct(r[0]) - target))[1]


def rate(rows, marks, fired, now, pct=None):
    """collect_rate's alert list, dropping the two diagnostic counters."""
    hits, _quiet, _unknown = ab.collect_rate(
        rows, marks, fired, now, ab.CRYPTO_RATE_PCT if pct is None else pct)
    return hits


def replay_near(stamp_from=None, pct=None):
    """Run the real record_marks/collect_rate over that morning.

    stamp_from limits when NEAR enters the tracked set, which is how the old
    code behaved - it only stamped coins that had already moved.

    Volume is supplied heavy enough to clear the confirmation gate, because
    what this replay tests is WHEN the price signal fires. The gate itself is
    tested on its own further down.
    """
    marks, fired = {}, {}
    for idx, hhmm in enumerate(RUN_TIMES):
        now = ct(hhmm)
        row = {"kind": "crypto", "symbol": "NEAR", "name": "NEAR Protocol",
               "price": near_price_at(hhmm), "pct": 5.0,
               "dollars": 1.6e9 * (1 + 0.05 * idx), "hour_pct": None}
        if stamp_from is None or ct(hhmm) >= ct(stamp_from):
            ab.record_marks([row], marks, now)
        hits = rate([row], marks, fired, now, pct)
        if hits:
            fkey, r, move, elapsed, rv = hits[0]
            fired[fkey] = now
            return hhmm, r["price"], move, elapsed
    return None


PEAK = 4.2779          # NEAR's high that morning
DAY_ALERT_PRICE = 4.23  # what the 15% day threshold got you, at 11:36

print(f"NEAR replay - at the live bar, CRYPTO_RATE_PCT = {ab.CRYPTO_RATE_PCT}%")
hit = replay_near()
check("fires before the 15% day alert at 11:36",
      hit is not None and ct(hit[0]) < ct("11:36"), f"got {hit}")
check("beats the price the day alert got you",
      hit is not None and hit[1] < DAY_ALERT_PRICE, f"got {hit}")
if hit:
    print(f"       -> {hit[0]} CT  ${hit[1]:.4f}  "
          f"+{hit[2]:.1f}% over {hit[3]:.0f} min"
          f"  |  {(PEAK/hit[1]-1)*100:.1f}% still to come"
          f"  (day alert: 11:36 @ $4.23, 1.2% left)")

# What the current bar costs. The most sensitive setting tested was 2%; if
# the live bar is higher, this is the price of the quiet, in minutes and in
# upside left on the table. Printed rather than asserted - the bar is Kyle's
# call, but it should never be invisible.
early = replay_near(pct=2.0)
if early and hit and early[0] != hit[0]:
    lost_min = (ct(hit[0]) - ct(early[0])) / 60
    print(f"       at 2%: {early[0]} CT  ${early[1]:.4f}  "
          f"({(PEAK/early[1]-1)*100:.1f}% still to come)"
          f"  ->  the current bar costs {lost_min:.0f} min "
          f"and {(hit[1]/early[1]-1)*100:.1f}% of entry price")

print("\nThe bug this fixes")
old = replay_near(stamp_from="11:02")
check("old behaviour (stamps only once already moving) misses it",
      old is None or ct(old[0]) >= ct("11:36"), f"got {old}")

print("\nThe measured window")
t0 = ct("10:00")
spike = {"kind": "crypto", "symbol": "SPK", "name": "Spike", "price": 100.0,
         "pct": 1.0, "dollars": 5e7, "hour_pct": None}
HEAVY = 1.30      # 30% more 24h volume across the window - about 7x normal

# A sharp move with no history behind it must NOT fire: the backtest found
# 10-20 minute spikes have no follow-through at all, so the window starts at
# 50 minutes and a stamp younger than that is ignored.
marks, fired = {}, {}
ab.record_marks([spike], marks, t0)
spike["price"] = 120.0
check("a 10-minute spike does not fire",
      rate([spike], marks, fired, t0 + 10 * 60) == [])

# A steady climb just over the configured bar must fire, and one just under
# it must not. Scaled to CRYPTO_RATE_PCT so these keep testing the mechanism
# rather than a threshold that moved.
over = ab.CRYPTO_RATE_PCT + 0.5
under = ab.CRYPTO_RATE_PCT - 0.5


def climb_fires(total_pct, vol_growth=HEAVY):
    marks, fired = {}, {}
    row = {"kind": "crypto", "symbol": "CRP", "name": "Creep", "price": 100.0,
           "pct": 1.0, "dollars": 5e7, "hour_pct": None}
    for i in range(0, 65, 8):
        row["price"] = 100.0 * (1 + (total_pct / 100.0) * (i / 64.0))
        row["dollars"] = 5e7 * (1 + (vol_growth - 1) * (i / 64.0))
        ab.record_marks([row], marks, t0 + i * 60)
    return rate([row], marks, fired, t0 + 64 * 60)


hits = climb_fires(over)
check(f"a steady climb of {over:.1f}%/hr fires", len(hits) == 1)
check("it reports a 50min+ window", hits and hits[0][3] >= 50)
check(f"a climb of {under:.1f}%/hr does not", climb_fires(under) == [])

# A stamp older than the window is ignored, so a stalled name cannot fire on
# ancient history.
marks, fired = {}, {}
stale = {"kind": "crypto", "symbol": "OLD", "name": "Stale", "price": 100.0,
         "pct": 1.0, "dollars": 5e7, "hour_pct": None}
ab.record_marks([stale], marks, t0)
stale["price"] = 130.0
check("a stamp older than the window is ignored",
      rate([stale], marks, fired, t0 + 200 * 60) == [])

print("\nCooldown and breadth")
# Stamp and check in the same order a live run does - record_marks prunes
# against the clock it is given, so building all the history first and then
# asking about an earlier moment tests nothing real.
marks, fired = {}, {}
runner = {"kind": "crypto", "symbol": "RUN2", "name": "Runner", "price": 100.0,
          "pct": 1.0, "dollars": 5e7, "hour_pct": None}
fires = []
for i in range(0, 400, 8):
    now = t0 + i * 60
    runner["price"] = 100.0 * (1 + 0.001 * i)     # +0.1%/min, a steady climb
    runner["dollars"] = 5e7 * (1 + 0.005 * i)     # and on rising volume
    ab.record_marks([runner], marks, now)
    for hit in rate([runner], marks, fired, now):
        fired[hit[0]] = now
        fires.append(i)
check("a steady climb fires", len(fires) >= 1, fires)
check("first fire once the window has history", fires and fires[0] >= 48, fires)
gaps = [b - a for a, b in zip(fires, fires[1:])]
check("never re-alerts inside the 3-hour cooldown",
      all(g >= 180 for g in gaps), gaps)
check("but does re-alert while it keeps climbing", len(fires) >= 2, fires)

universe = [{"symbol": f"C{i}"} for i in range(100)]
check("a melt-up is suppressed", ab.too_broad(list(range(60)), universe))
check("an ordinary busy cycle is not", not ab.too_broad(list(range(40)), universe))
check("a handful of movers is not", not ab.too_broad(list(range(5)), universe))
check("suppression needs a real universe", not ab.too_broad([1, 2], universe))

print("\nUniverse vs candidates")


def fake_markets(payload):
    class Resp:
        def __enter__(self_in): return self_in
        def __exit__(self_in, *a): return False
        def read(self_in): return json.dumps(payload).encode()
    return lambda req, timeout=None: Resp()


coins = [
    {"symbol": "flat", "name": "Flat Coin", "current_price": 10.0,
     "total_volume": 5e8, "market_cap": 1e9,
     "price_change_percentage_24h_in_currency": 0.4,
     "price_change_percentage_1h_in_currency": 0.1},
    {"symbol": "run", "name": "Runner", "current_price": 2.0,
     "total_volume": 5e8, "market_cap": 1e9,
     "price_change_percentage_24h_in_currency": 22.0,
     "price_change_percentage_1h_in_currency": 3.0},
    {"symbol": "usdt", "name": "Tether", "current_price": 1.0,
     "total_volume": 9e9, "market_cap": 9e10,
     "price_change_percentage_24h_in_currency": 0.0,
     "price_change_percentage_1h_in_currency": 0.0},
    {"symbol": "tiny", "name": "Illiquid", "current_price": 1.0,
     "total_volume": 1e5, "market_cap": 1e6,
     "price_change_percentage_24h_in_currency": 80.0,
     "price_change_percentage_1h_in_currency": 40.0},
    {"symbol": "龙虾", "name": "Lobster", "current_price": 1.0,
     "total_volume": 5e8, "market_cap": 1e9,
     "price_change_percentage_24h_in_currency": 50.0,
     "price_change_percentage_1h_in_currency": 20.0},
    {"symbol": "null", "name": "No Data", "current_price": 5.0,
     "total_volume": 5e8, "market_cap": 1e9,
     "price_change_percentage_24h_in_currency": None,
     "price_change_percentage_24h": None,
     "price_change_percentage_1h_in_currency": None},
]
real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_markets(coins)
try:
    rows = ab.screen_crypto()
finally:
    urllib.request.urlopen = real_urlopen

syms = {r["symbol"] for r in rows}
cands = {r["symbol"] for r in rows if r["candidate"]}
check("a flat liquid coin IS tracked", "FLAT" in syms)
check("a flat liquid coin is NOT a candidate", "FLAT" not in cands)
check("a real mover is both", "RUN" in syms and "RUN" in cands)
check("the misleading 1h figure is out of the alert body",
      "1h" not in ab.format_body({"kind": "crypto", "symbol": "X", "name": "X",
                                  "pct": 9.0, "price": 2.0, "dollars": 5e7,
                                  "hour_pct": 5.3}))
check("stablecoins are excluded", "USDT" not in syms)
check("illiquid coins are excluded", "TINY" not in syms)
check("non-ticker symbols are excluded", "龙虾" not in syms and len(syms) == 2,
      sorted(syms))
check("a null change percent does not crash", "NULL" not in syms)

print("\nRobinhood's own list")


def rh_payload(n, tradable=True):
    return {"results": [
        {"tradability": "tradable" if tradable else "untradable",
         "asset_currency": {"code": f"C{i}", "name": f"Coin {i}"}}
        for i in range(n)]}


def with_urlopen(payload, fn):
    real = urllib.request.urlopen
    urllib.request.urlopen = fake_markets(payload)
    try:
        return fn()
    finally:
        urllib.request.urlopen = real


syms = with_urlopen(rh_payload(40), ab.robinhood_symbols)
check("parses the tradable codes", syms is not None and len(syms) == 40)
check("codes are upper-cased", syms and "C1" in syms)
check("untradable pairs are excluded",
      with_urlopen(rh_payload(40, tradable=False), ab.robinhood_symbols) is None)
check("a suspiciously short list returns None, not an empty universe",
      with_urlopen(rh_payload(5), ab.robinhood_symbols) is None)
check("a broken payload returns None",
      with_urlopen({"nope": 1}, ab.robinhood_symbols) is None)

# The filter has to actually narrow the universe, and None must not narrow it.
rows_all = with_urlopen(coins, lambda: ab.screen_crypto(None))
rows_rh = with_urlopen(coins, lambda: ab.screen_crypto({"RUN"}))
check("no list means no extra filtering", {r["symbol"] for r in rows_all} == {"FLAT", "RUN"})
check("a list narrows the tracked universe", {r["symbol"] for r in rows_rh} == {"RUN"})

print("\nVolume confirmation")

# The gate exists because, scored against a real day of alerts, names moving
# on ordinary volume went nowhere far more often. These check the arithmetic
# that turns each source's awkward counter into a window relative volume.

WINDOW = 70.0   # minutes, the middle of the live window


def crypto_rv(vol_growth):
    """relative_volume for a coin whose 24h dollar volume grew by this much."""
    row = {"kind": "crypto", "symbol": "C", "price": 1.0, "pct": 1.0,
           "dollars": 1e8 * vol_growth, "hour_pct": None}
    return ab.relative_volume(row, (0, 1.0, WINDOW, 1e8))


# A rolling 24h total that has not moved means the last hour was a normal
# hour, whatever the price did.
check("flat 24h volume reads as ~1x", abs(crypto_rv(1.0) - 1.0) < 0.01)
check("a 10% jump in 24h volume is a big surge", crypto_rv(1.10) > 3)
# How big a jump in the 24h total the live bar actually demands. Derived from
# the bar rather than hardcoded, so moving RATE_MIN_RELVOL does not break this.
needed = (ab.RATE_MIN_RELVOL - 1) * (WINDOW / 1440.0)
check(f"the live {ab.RATE_MIN_RELVOL:g}x bar needs about a {needed * 100:.0f}%"
      f" jump in 24h volume",
      crypto_rv(1 + needed * 0.9) < ab.RATE_MIN_RELVOL <= crypto_rv(1 + needed * 1.1),
      f"{crypto_rv(1 + needed * 0.9):.2f} / {crypto_rv(1 + needed * 1.1):.2f}")
check("shrinking volume reads below 1x", crypto_rv(0.98) < 1)


def stock_rv(shares_in_window, avg_daily):
    row = {"kind": "stock", "symbol": "S", "price": 10.0, "pct": 1.0,
           "volume": 1e6 + shares_in_window, "avg_volume": avg_daily}
    return ab.relative_volume(row, (0, 10.0, WINDOW, 1e6))


# A normal 70 minutes is 70/390 of an average day.
normal = 1e7 * (WINDOW / ab.SESSION_MINUTES)
check("an average pace reads as ~1x", abs(stock_rv(normal, 1e7) - 1.0) < 0.01)
check("five times the pace reads as 5x",
      abs(stock_rv(5 * normal, 1e7) - 5.0) < 0.01)
check("a session reset (counter goes backwards) is unknown, not zero",
      ab.relative_volume({"kind": "stock", "symbol": "S", "price": 10.0,
                          "pct": 1.0, "volume": 5e5, "avg_volume": 1e7},
                         (0, 10.0, WINDOW, 1e6)) is None)
check("no average volume is unknown, not a divide by zero",
      stock_rv(normal, 0) is None)

# Missing data must not alert, and must be counted rather than silent.
marks, fired = {}, {}
novol = {"kind": "crypto", "symbol": "NV", "name": "No Volume", "price": 100.0,
         "pct": 1.0, "dollars": None, "hour_pct": None}
for i in range(0, 65, 8):
    novol["price"] = 100.0 * (1 + 0.001 * i)
    ab.record_marks([novol], marks, t0 + i * 60)
hits, quiet, unknown = ab.collect_rate([novol], marks, fired, t0 + 64 * 60,
                                       ab.CRYPTO_RATE_PCT)
check("a name with no volume figure does not alert", hits == [])
check("...and is counted as unknown, not quietly dropped", unknown == 1)

# A real climb on ordinary volume is held back, and counted.
hits2 = climb_fires(ab.CRYPTO_RATE_PCT + 0.5, vol_growth=1.0)
check("a climb on ordinary volume does not alert", hits2 == [])

# Stamps written before volume tracking existed must not crash or alert.
old_style = {"crypto:OLD2": [[t0, 100.0], [t0 + 30 * 60, 103.0]]}
mark = ab.oldest_mark(old_style["crypto:OLD2"], t0 + 60 * 60, 50, 75)
check("a pre-volume stamp still parses", mark is not None and mark[3] is None)
check("...and yields no volume reading",
      ab.relative_volume({"kind": "crypto", "symbol": "OLD2", "price": 110.0,
                          "pct": 1.0, "dollars": 2e8, "hour_pct": None},
                         mark) is None)

print("\nStock price band")

# Kyle only wants stocks under $10. The cap has to reach the Yahoo query, not
# just the local check: the screener returns at most 250 names sorted by
# percent change, so filtering afterwards would waste most of that budget on
# names too expensive to alert on.


class FakeYF:
    """Stands in for the yfinance module, capturing the query it is handed."""

    def __init__(self, quotes):
        self.quotes = quotes
        self.queries = []

    def screen(self, query, offset=0, size=250, **kw):
        self.queries.append(query.to_dict())
        return {"quotes": self.quotes if offset == 0 else []}


def quote(sym, price, chg=4.0, vol=12_000_000):
    return {"symbol": sym, "shortName": sym, "regularMarketChangePercent": chg,
            "regularMarketPrice": price, "regularMarketVolume": vol,
            "marketCap": 500_000_000, "averageDailyVolume3Month": 1_000_000,
            "financialCurrency": "USD"}


try:
    import yfinance  # noqa: F401
except ImportError:
    print("  skip  yfinance not installed")
else:
    yf = FakeYF([
        quote("TOOCHEAP", 2.50),
        quote("LOW", 3.10),
        quote("MID", 7.58),
        quote("EDGE", 9.99),
        quote("OVER", 10.01),
        quote("WAYOVER", 268.34),
    ])
    rows = ab.screen_stocks(yf, ab.STOCK_TRACK_FLOOR)
    got = {r["symbol"] for r in rows}
    check("keeps stocks inside the price band", got == {"LOW", "MID", "EDGE"}, sorted(got))
    check("drops anything at or over the cap", "OVER" not in got and "WAYOVER" not in got)
    check("still drops penny stocks under the floor", "TOOCHEAP" not in got)

    # The cap must be in the query Yahoo actually receives.
    ops = yf.queries[0]["operands"]
    price_terms = [o for o in ops if o["operands"][0] == "intradayprice"]
    check("the query carries both a floor and a cap", len(price_terms) == 2,
          price_terms)
    check("the cap is sent as a less-than on intradayprice",
          any(o["operator"] == "LT" and o["operands"][1] == ab.MAX_PRICE
              for o in price_terms), price_terms)
    check("the floor is still sent",
          any(o["operator"] == "GT" and o["operands"][1] == ab.MIN_PRICE
              for o in price_terms), price_terms)
    check("the band is sane", ab.MIN_PRICE < ab.MAX_PRICE)

    # Worth pinning because it surprised me: MIN_DOLLAR_VOLUME bites much
    # harder once the price cap is on. A $10 name clears $25M on 2.5M shares;
    # a $3 name needs over 8M. Cheap stocks are not automatically in.
    thin = FakeYF([quote("THIN", 3.10, vol=5_000_000)])
    check("a cheap stock still has to trade real money",
          ab.screen_stocks(thin, ab.STOCK_TRACK_FLOOR) == [])
    shares_needed = ab.MIN_DOLLAR_VOLUME / ab.MAX_PRICE
    check(f"at the ${ab.MAX_PRICE:.0f} cap that means {shares_needed/1e6:.1f}M+ shares",
          shares_needed > 1_000_000)

print("\nStale marks")
marks = {"crypto:OLD": [[t0 - 3 * 3600, 1.0]], "crypto:NEW": [[t0, 1.0]]}
ab.drop_stale_marks(marks, t0)
check("stale names are forgotten", list(marks) == ["crypto:NEW"])

print("\nCoinbase volume confirmation")

# 25 hours of 5-minute candles at a flat $1,000/candle, then a 70-minute
# window at 4x that pace. Coinbase rows are [time, low, high, open, close, vol].
NOW = 1_800_000_000
def cb_candles(window_mult, minutes=70, hours=25, gap_hours=0):
    rows = []
    for k in range(hours * 12):
        t = NOW - (k + 1) * 300
        if gap_hours and t < NOW - minutes * 60 and t > NOW - (minutes * 60 + gap_hours * 3600):
            continue                        # a quiet stretch Coinbase left out
        vol = 1000.0 * (window_mult if t >= NOW - minutes * 60 else 1.0)
        rows.append([t, 1.0, 1.0, 1.0, 1.0, vol])
    return rows

rv = ab.relvol_from_candles(cb_candles(4.0), 70, NOW)
check("a 4x window reads as ~4x", rv is not None and abs(rv - 4.0) < 0.05, rv)
rv1 = ab.relvol_from_candles(cb_candles(1.0), 70, NOW)
check("an ordinary window reads as ~1x", rv1 is not None and abs(rv1 - 1.0) < 0.05, rv1)
# Missing candles are zero-volume periods, so the baseline is measured by
# time span - a 6-hour hole must LOWER normal pace, not raise it.
rv_gap = ab.relvol_from_candles(cb_candles(1.0, gap_hours=6), 70, NOW)
check("omitted candles count as quiet time, not as missing time",
      rv_gap is not None and rv_gap > 1.2, rv_gap)
check("too little history gives no opinion",
      ab.relvol_from_candles(cb_candles(4.0, hours=3), 70, NOW) is None)
check("garbage rows are skipped, not fatal",
      ab.relvol_from_candles(cb_candles(4.0) + [["x"], None, [1, 2]], 70, NOW) is not None)
check("empty input gives None", ab.relvol_from_candles([], 70, NOW) is None)

# The network wrapper: a 404 is remembered for the day, any other trouble
# just means no opinion (the CoinGecko fallback takes over).
import urllib.error

def http_error(code):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "nope", {}, None)
    return opener

real_cb = REAL_COINBASE_RELVOL        # the suite pins the module's copy to None above

missing = {}
real_open = urllib.request.urlopen
urllib.request.urlopen = http_error(404)
try:
    got = real_cb("NOPE", 70, NOW, missing)
finally:
    urllib.request.urlopen = real_open
check("a 404 gives no opinion", got is None)
check("...and is remembered so the coin is not asked about again today", missing == {"NOPE": True})
calls = []
urllib.request.urlopen = lambda req, timeout=None: calls.append(req) or (_ for _ in ()).throw(RuntimeError("should not be called"))
try:
    got = real_cb("NOPE", 70, NOW, missing)
finally:
    urllib.request.urlopen = real_open
check("a remembered miss costs no request", got is None and calls == [])
urllib.request.urlopen = http_error(500)
try:
    got = real_cb("ERR", 70, NOW, missing)
finally:
    urllib.request.urlopen = real_open
check("a server error is not remembered as a missing market", got is None and "ERR" not in missing)
urllib.request.urlopen = fake_markets(cb_candles(4.0))
try:
    got = real_cb("GOOD", 70, NOW, {})
finally:
    urllib.request.urlopen = real_open
check("a real candle payload comes back as the window multiple",
      got is not None and abs(got - 4.0) < 0.05, got)

# collect_rate must PREFER Coinbase when it has an opinion, both ways round.
def climb_with(cb_answer, cg_growth):
    marks, fired = {}, {}
    row = {"kind": "crypto", "symbol": "CB", "name": "Test", "price": 100.0,
           "pct": 2.0, "dollars": 1e8, "hour_pct": None}
    ab.record_marks([row], marks, t0)
    row = dict(row, price=104.0, dollars=1e8 * cg_growth)
    saved = ab.coinbase_relvol
    ab.coinbase_relvol = lambda sym, minutes, now_ts, missing=None: cb_answer
    try:
        hits, quiet, unknown = ab.collect_rate([row], marks, fired, t0 + 60 * 60,
                                               ab.CRYPTO_RATE_PCT)
    finally:
        ab.coinbase_relvol = saved
    return hits, quiet, row.get("rv_source")

hits, quiet, src = climb_with(4.0, 1.0)      # Coinbase busy, CoinGecko flat
check("Coinbase volume can confirm a climb CoinGecko calls ordinary",
      len(hits) == 1 and src == "coinbase", (len(hits), quiet, src))
hits, quiet, src = climb_with(1.0, 1.5)      # Coinbase quiet, CoinGecko busy
check("...and Coinbase can veto one CoinGecko would have sent",
      hits == [] and quiet == 1 and src == "coinbase", (len(hits), quiet, src))
hits, quiet, src = climb_with(None, 1.5)     # no Coinbase market
check("with no Coinbase opinion the CoinGecko figure decides",
      len(hits) == 1 and src == "coingecko", (len(hits), quiet, src))
check("the CoinGecko figure is what gets reported in that case",
      hits and hits[0][4] > ab.RATE_MIN_RELVOL)

print("\nAlert tags: first buzz, repeats, days running")
st = {"alerts_today": {}, "streak": {}}
noon = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc).timestamp()   # noon CT
lines = ab.tag_lines(st, "crypto:QNT", noon)
check("the first alert says so", lines == ["1st alert today"], lines)
lines = ab.tag_lines(st, "crypto:QNT", noon + 3600)
check("the second says repeat #2 and when the first was",
      lines == ["alert #2 today (first 12:00pm)"], lines)
lines = ab.tag_lines(st, "crypto:QNT", noon + 7200)
check("...and keeps counting", lines and lines[0].startswith("alert #3 today"), lines)
check("a day-1 name carries no streak line", len(lines) == 1, lines)
check("no state means no lines and no crash", ab.tag_lines(None, "crypto:QNT", noon) == [])

# "Today" is Kyle's calendar day. The bot's state resets at midnight UTC,
# which is 7pm Central, so the send log is carried across that reset and the
# count filters by Central date instead.
st = {"alerts_today": {"crypto:QNT": [noon - 20 * 3600]}, "streak": {}}   # 4pm yesterday CT
lines = ab.tag_lines(st, "crypto:QNT", noon)
check("yesterday evening's alert does not count toward today", lines[0] == "1st alert today", lines)
evening = datetime(2026, 9, 28, 1, 30, tzinfo=timezone.utc).timestamp()     # 8:30pm CT 09-27, new UTC day
st = {"alerts_today": {"crypto:QNT": [noon]}, "streak": {}}
lines = ab.tag_lines(st, "crypto:QNT", evening)
check("an alert after 7pm CT still counts the afternoon one",
      lines[0] == "alert #2 today (first 12:00pm)", lines)
pruned = ab.prune_alert_log({"a": [noon - 40 * 3600, noon - 3600], "b": [noon - 50 * 3600], "c": "junk"}, noon)
check("old stamps and empty names are pruned from the send log",
      pruned == {"a": [noon - 3600]}, pruned)

# Days running, in Central days. Three alerts on one day are one day.
streak = {}
check("first day", ab.note_streak(streak, "crypto:QNT", "2026-09-24") == 1)
check("same day again does not inflate", ab.note_streak(streak, "crypto:QNT", "2026-09-24") == 1)
check("next day makes two", ab.note_streak(streak, "crypto:QNT", "2026-09-25") == 2)
check("and three", ab.note_streak(streak, "crypto:QNT", "2026-09-26") == 3)
check("first day is remembered", streak["crypto:QNT"]["first"] == "2026-09-24")
check("a skipped day starts over", ab.note_streak(streak, "crypto:QNT", "2026-09-28") == 1)
check("the streak line names the day count and the first weekday",
      ab.streak_line(3, "2026-09-24") == "still running: day 3 of alerts (first Thu)",
      ab.streak_line(3, "2026-09-24"))
check("no line on day one", ab.streak_line(1, "2026-09-24") is None)
kept = ab.prune_streak({"a": {"first": "2026-09-20", "last": "2026-09-20", "days": 1},
                        "b": {"first": "2026-09-25", "last": "2026-09-26", "days": 2},
                        "bad": {"nope": 1}}, "2026-09-27")
check("old and broken entries are pruned, recent ones kept", list(kept) == ["b"], kept)

# The memory must survive the daily reset that wipes everything else.
import tempfile, os
tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
recent = datetime.now(timezone.utc).timestamp() - 3600
json.dump({"date": "2000-01-01", "fired": {"x": 1}, "marks": {"m": []},
           "alerts_today": {"crypto:QNT": [recent, recent - 90 * 3600]},
           "streak": {"crypto:QNT": {"first": ab.local_date(), "last": ab.local_date(), "days": 2}}}, tmp)
tmp.close()
saved_path = ab.STATE_FILE
ab.STATE_FILE = ab.Path(tmp.name)
try:
    fresh = ab.load_state()
finally:
    ab.STATE_FILE = saved_path
    os.unlink(tmp.name)
check("a new day starts with empty fired/marks", fresh["fired"] == {} and fresh["marks"] == {})
check("...but keeps the days-running memory", fresh["streak"].get("crypto:QNT", {}).get("days") == 2, fresh["streak"])
check("and has the new buckets", "alerts_today" in fresh and "cb_missing" in fresh)
check("...and the recent send log, minus stale stamps", fresh["alerts_today"] == {"crypto:QNT": [recent]}, fresh["alerts_today"])

# End to end: the lines reach the notification body.
sent = []
real_push = ab.push
ab.push = lambda title, message, **k: sent.append((title, message))
try:
    st = {"alerts_today": {}, "streak": {"crypto:QNT": {"first": "2026-09-24", "last": ab.local_date(noon - 86400), "days": 3}}}
    row = {"kind": "crypto", "symbol": "QNT", "name": "Quant", "price": 168.6,
           "pct": 57.6, "low_24h": 109.0, "rv_source": "coinbase"}
    ab.send_rate([("rate:crypto:QNT", row, 5.9, 69.0, 3.2)], {}, noon, False, state=st)
finally:
    ab.push = real_push
body = sent[0][1] if sent else ""
check("rate alert body carries the first-buzz line", "1st alert today" in body, body)
check("...and the days-running line", "still running: day 4 of alerts (first Thu)" in body, body)
check("the old lines are still there", "break-even" in body and "off 24h low" in body)

print("\nAlert tiers: the star, the caution, the priorities")
check("stop hint is the 15% the 60-day study picked", ab.STOP_HINT_PCT == 15.0, ab.STOP_HINT_PCT)
be, stop = ab.cost_hints(100.0)
check("15% stop hint lands 15% under, then under Robinhood's markdown",
      abs(stop - 100.0 * 0.85 * (1 - ab.EXEC_MARKUP_PCT / 100)) < 1e-9, stop)

def crow(**kw):
    r = {"kind": "crypto", "symbol": "T", "name": "Test", "price": 1.0, "pct": 2.0,
         "week_pct": 5.0, "rv_source": "coinbase"}
    r.update(kw)
    return r

top, lines = ab.tier_lines(crow(), 6.0, True)
check("first alert, quiet week, day +2%, 6x Coinbase volume is a TOP SETUP", top and lines[0].startswith("TOP SETUP"), lines)
check("...with no caution", len(lines) == 1, lines)
top, lines = ab.tier_lines(crow(), 6.0, False)
check("a repeat is never a top setup", not top and lines == [], lines)
top, lines = ab.tier_lines(crow(pct=12.0), 6.0, True)
check("day +12% is not a top setup", not top, lines)
top, lines = ab.tier_lines(crow(), 4.9, True)
check("4.9x volume is not a top setup", not top, lines)
top, lines = ab.tier_lines(crow(rv_source="coingecko"), 9.0, True)
check("the star needs Coinbase-confirmed volume", not top, lines)
top, lines = ab.tier_lines(crow(week_pct=57.0), 6.0, True)
check("a hot week gets the information line even on a top setup",
      top and any(l.startswith("hot week: up 57% on the week") for l in lines), lines)
top, lines = ab.tier_lines(crow(week_pct=19.9), 2.6, False)
check("just under the week bar: no caution", lines == [], lines)
top, lines = ab.tier_lines(crow(week_pct=None), 2.6, False)
check("no week figure: no caution, no crash", lines == [], lines)
top, lines = ab.tier_lines({"kind": "stock", "symbol": "S", "pct": 3.0, "week_pct": 30.0}, 6.0, True)
check("stocks get the caution but never the star", not top and len(lines) == 1, lines)

# The whole thing through send_rate: title, tag, priority.
sent = []
real_push, real_book = ab.push, ab.load_book
ab.push = lambda title, message, **k: sent.append((title, message, k.get("priority"), k.get("tags")))
ab.load_book = lambda: OPEN_BOOK
try:
    st = {"alerts_today": {}, "streak": {}}
    r1 = {"kind": "crypto", "symbol": "GRT", "name": "The Graph", "price": 0.0295, "pct": 6.3,
          "week_pct": 8.0, "low_24h": 0.0271, "rv_source": "coinbase"}
    ab.send_rate([("rate:crypto:GRT", r1, 3.9, 71.0, 6.2)], {}, noon, False, state=st)
    ab.send_rate([("rate:crypto:GRT", dict(r1), 4.8, 71.0, 6.0)], {}, noon + 3600, False, state=st)
    r2 = {"kind": "crypto", "symbol": "QNT", "name": "Quant", "price": 168.6, "pct": 57.6,
          "week_pct": 130.0, "low_24h": 109.0, "rv_source": "coinbase"}
    ab.send_rate([("rate:crypto:QNT", r2, 5.9, 69.0, 3.2)], {}, noon, False, state=st)
    ab.send_rate([("rate:crypto:QNT", dict(r2), 5.9, 69.0, 3.2)], {}, noon - 6 * 3600, True, state={"alerts_today": {}, "streak": {}})
finally:
    ab.push, ab.load_book = real_push, real_book
t1, b1, p1, g1 = sent[0]
check("first GRT alert is a TOP SETUP, alert only since 10-16: no star, quiet feed",
      not t1.startswith("*") and p1 == "low" and g1 == "zap", (t1, p1, g1))
check("...with no lot size in the title", "lot" not in t1, t1)
check("...and the body names it, says it is not a buy signal, and quotes a 15% stop",
      "TOP SETUP (alert only" in b1 and "not a buy signal" in b1 and "15% stop" in b1, b1)
t2, b2, p2, g2 = sent[1]
check("the repeat an hour later is unstarred and goes to the quiet feed", not t2.startswith("*") and p2 == "low" and "alert #2 today" in b2, (t2, p2, b2))
t3, b3, p3, g3 = sent[2]
check("Quant up 130% on the week carries the hot-week line, no star, quiet feed", "hot week: up 130% on the week" in b3 and not t3.startswith("*") and p3 == "low", (t3, p3, b3))
t4, b4, p4, g4 = sent[3]
check("an overnight mover goes to the quiet feed, not max", p4 == "low", p4)

# The 7d figure has to come through the screener.
rows7 = with_urlopen([dict(coins[1], price_change_percentage_7d_in_currency=34.5)], lambda: ab.screen_crypto(None))
check("screen_crypto carries the week change", rows7 and rows7[0].get("week_pct") == 34.5, rows7 and rows7[0].get("week_pct"))
check("...and asks CoinGecko for it", "7d" in ab.COINGECKO_URL, ab.COINGECKO_URL)
rows_none = with_urlopen([dict(coins[1])], lambda: ab.screen_crypto(None))
check("a missing week figure is None, not a crash", rows_none and rows_none[0].get("week_pct") is None)


print("\nEvery Robinhood coin, not just the top 250")
top_page = [dict(coins[1])]                      # RUN is in the top page
tail_coin = {"symbol": "zora", "name": "Zora", "current_price": 0.02,
             "total_volume": 1.3e7, "market_cap": 3.5e7,
             "price_change_percentage_24h_in_currency": 6.0,
             "price_change_percentage_7d_in_currency": 12.0}
thin_coin = dict(tail_coin, symbol="mew", total_volume=1.5e6)
check("missing = Robinhood coins not on the page, stables aside",
      ab.missing_robinhood(top_page, {"RUN", "ZORA", "USDC", "MEW"}) == ["MEW", "ZORA"],
      ab.missing_robinhood(top_page, {"RUN", "ZORA", "USDC", "MEW"}))
check("nothing missing without a Robinhood list", ab.missing_robinhood(top_page, None) == [])

asked = []
def two_calls(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    asked.append(url)
    body = [tail_coin, thin_coin, dict(coins[1])] if "symbols=" in url else top_page
    return fake_markets(body)(req, timeout)
real = urllib.request.urlopen
urllib.request.urlopen = two_calls
try:
    rows_tail = ab.screen_crypto({"RUN", "ZORA", "MEW"})
finally:
    urllib.request.urlopen = real
syms_tail = sorted(r["symbol"] for r in rows_tail)
check("the tail is fetched by symbol", len(asked) == 2 and "symbols=mew,zora" in asked[1], asked)
check("a $35M-cap, $13M-volume coin is now tracked", "ZORA" in syms_tail, syms_tail)
check("a coin under $2M volume still is not", "MEW" not in syms_tail, syms_tail)
check("no duplicate when the tail repeats a top coin", syms_tail.count("RUN") == 1, syms_tail)
check("rows carry market cap", [r for r in rows_tail if r["symbol"] == "ZORA"][0]["cap"] == 3.5e7)

calls2 = []
def tail_fails(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    calls2.append(url)
    if "symbols=" in url:
        raise OSError("rate limited")
    return fake_markets(top_page)(req, timeout)
urllib.request.urlopen = tail_fails
try:
    rows_f = ab.screen_crypto({"RUN", "ZORA"})
finally:
    urllib.request.urlopen = real
check("a failed tail fetch keeps the top page", [r["symbol"] for r in rows_f] == ["RUN"], rows_f)

calls3 = []
def one_call(req, timeout=None):
    calls3.append(req)
    return fake_markets(top_page)(req, timeout)
urllib.request.urlopen = one_call
try:
    ab.screen_crypto({"RUN"})
finally:
    urllib.request.urlopen = real
check("no second request when nothing is missing", len(calls3) == 1, len(calls3))

top, lines = ab.tier_lines(crow(cap=3.5e7), 2.6, False)
check("small coins carry the stop warning",
      any("small coin ($35M cap)" in l for l in lines), lines)
top, lines = ab.tier_lines(crow(cap=5e8), 2.6, False)
check("larger coins do not", not any("small coin" in l for l in lines), lines)
top, lines = ab.tier_lines({"kind": "stock", "symbol": "S", "pct": 3.0, "cap": 1e7}, 6.0, True)
check("stocks never get the crypto small-coin line", not any("small coin" in l for l in lines), lines)

# --- A failed crypto screen is an outage, not a quiet market (2026-10-07) ---
# The 11:08 UTC run on 10-07 logged "crypto screener failed: HTTP Error 429"
# and then read as a healthy, quiet run: "tracking 0 liquid coins", "daily
# context for 82 of 0 coins", and a watchlist line claiming BTC was not
# tradable. screen_crypto now returns None so the caller can tell the two
# apart.
def raising_urlopen(*a, **k):
    raise urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)

_real = urllib.request.urlopen
urllib.request.urlopen = raising_urlopen
try:
    failed = ab.screen_crypto(None)
finally:
    urllib.request.urlopen = _real
check("a screen that could not be read returns None, not an empty universe",
      failed is None, failed)
check("a broken screen payload returns None too",
      with_urlopen({"nope": 1}, lambda: ab.screen_crypto(None)) is None)

st = {"screen_fails": 0}
sent_pushes = []
_rp = ab.push
ab.push = lambda title, message, **kw: sent_pushes.append((title, kw))
try:
    n1 = ab.crypto_screen_failed(st)
    n2 = ab.crypto_screen_failed(st)
    check("the failure streak counts up", (n1, n2) == (1, 2), (n1, n2))
    check("no push before the streak is reached", sent_pushes == [], sent_pushes)
    n3 = ab.crypto_screen_failed(st)
    check("pushes once when the streak is reached",
          n3 == ab.SCREEN_FAIL_ALERT_RUNS and len(sent_pushes) == 1
          and sent_pushes[0][1].get("priority") == "high", sent_pushes)
    ab.crypto_screen_failed(st)
    check("and not again while the outage lasts", len(sent_pushes) == 1, sent_pushes)
    ab.crypto_screen_recovered(st)
    check("recovery clears the streak", st["screen_fails"] == 0, st)
    ab.crypto_screen_failed(st)
    check("a later outage can push again",
          st["screen_fails"] == 1 and len(sent_pushes) == 1, st)
finally:
    ab.push = _rp

print("\nQuieter alerts (2026-10-16): only a buyable star buzzes")
NOON_CT = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc).timestamp()   # noon CDT
NINE_PM = datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc).timestamp()    # 9 PM CDT
SEVEN_AM = datetime(2026, 10, 17, 12, 5, tzinfo=timezone.utc).timestamp()  # 7:05 AM CDT


def lot(sym, qty, cost, stop, bought="2026-10-10", routing="market_maker", product=None):
    return {"symbol": sym, "product": product or f"{sym.split('#')[0]}-USD", "qty": qty,
            "avg_cost": cost, "current_stop": stop, "routing": routing, "bought": bought}


check("noon is inside the buy window, 9 PM and 6 AM are not",
      ab.in_buy_window(NOON_CT) and not ab.in_buy_window(NINE_PM)
      and not ab.in_buy_window(SEVEN_AM - 3600))
check("open book: a new coin gets the lot size",
      ab.buy_gate("PYTH", NOON_CT, OPEN_BOOK) == (True, "$500 lot"), ab.buy_gate("PYTH", NOON_CT, OPEN_BOOK))
check("after 7 PM: no buys", ab.buy_gate("PYTH", NINE_PM, OPEN_BOOK)[0] is False
      and "outside 7 AM-7 PM" in ab.buy_gate("PYTH", NINE_PM, OPEN_BOOK)[1])
paused = dict(OPEN_BOOK, _buys_allowed=False, _buys_note="over the open-risk limit")
check("the switch off: no buys, with its note",
      ab.buy_gate("PYTH", NOON_CT, paused) == (False, "over the open-risk limit"))
full = dict(OPEN_BOOK, positions=[lot(f"C{i}", 1, 10.0, 8.0) for i in range(10)])
check("ten coins held: no buys", ab.buy_gate("PYTH", NOON_CT, full) == (False, "10 coins held, max 10"),
      ab.buy_gate("PYTH", NOON_CT, full))
stacked = dict(OPEN_BOOK, positions=[lot(f"C{i}", 1, 10.0, 8.0) for i in range(5)]
               + [lot(f"C{i}#{k}", 1, 10.0, 8.0) for i in range(3) for k in (2, 3)])    # 11 lots, 5 coins
check("slots are coins, not lots: eleven lots on five coins leave room (2026-10-19)",
      ab.buy_gate("PYTH", NOON_CT, stacked) == (True, "$500 lot"), ab.buy_gate("PYTH", NOON_CT, stacked))
check("...and a held coin's second lot is judged by the add rule, not the slot count",
      ab.buy_gate("C0", NOON_CT, dict(full, positions=full["positions"] + [lot("C0#2", 1, 10.0, 8.0)]))
      == (False, "held, first lot not locked: no add"))
check("a lower cap in the book is honoured; a missing one means ten",
      ab.buy_gate("PYTH", NOON_CT, dict(full, _max_open_coins=12)) == (True, "$500 lot")
      and ab.buy_gate("PYTH", NOON_CT, dict(stacked, _max_open_coins=5)) == (False, "5 coins held, max 5")
      and "_max_open_coins" not in full and ab.DEFAULT_MAX_COINS == 10)
heavy = dict(OPEN_BOOK, positions=[lot("BAT", 100000, 0.396, 0.3)])
check("at the $40K crypto cap: no buys", ab.buy_gate("PYTH", NOON_CT, heavy)[0] is False
      and "cap" in ab.buy_gate("PYTH", NOON_CT, heavy)[1], ab.buy_gate("PYTH", NOON_CT, heavy))
unlocked = dict(OPEN_BOOK, positions=[lot("PYTH", 5500, 0.0899, 0.0756), lot("PYTH#2", 4500, 0.0836, 0.0703)])
check("held and not locked: no add",
      ab.buy_gate("PYTH", NOON_CT, unlocked) == (False, "held, first lot not locked: no add"))
locked = dict(OPEN_BOOK, positions=[lot("BAT", 50000, 0.1279, 0.2027), lot("BAT#4", 50000, 0.1816, 0.2027)])
check("held and locked: still not an alert buy - adds come only from a calm stop check",
      ab.buy_gate("BAT", NOON_CT, locked) == (False, "held: adds only at a calm stop check"),
      ab.buy_gate("BAT", NOON_CT, locked))
today = dict(OPEN_BOOK, positions=[lot("BAT", 50000, 0.1279, 0.2027, bought="2026-10-16")])
check("bought today: not a second lot", ab.buy_gate("BAT", NOON_CT, today) == (False, "already bought today"))
real_book = ab.load_book
ab.load_book = lambda: None
check("unreadable book: no buys", ab.buy_gate("PYTH", NOON_CT) == (False, "positions file unreadable"))
ab.load_book = real_book
check("buy_decision gives the size too, halved for a yellow flag",
      ab.buy_decision("PYTH", NOON_CT, OPEN_BOOK) == (True, "$500 lot", 500.0)
      and ab.buy_decision("PYTH", NOON_CT, OPEN_BOOK, half="high-vol coin")
      == (True, "$250 lot (half size: high-vol coin)", 250.0),
      ab.buy_decision("PYTH", NOON_CT, OPEN_BOOK, half="high-vol coin"))
nine = dict(OPEN_BOOK, positions=[lot(f"C{i}", 1, 10.0, 8.0) for i in range(9)])
check("a star earlier in the same run counts as a coin",
      ab.buy_gate("PYTH", NOON_CT, nine, pending=[{"symbol": "GRT", "usd": 500.0}])
      == (False, "9 coins held + 1 starred this run, max 10")
      and ab.buy_gate("PYTH", NOON_CT, nine) == (True, "$500 lot"),
      ab.buy_gate("PYTH", NOON_CT, nine, pending=[{"symbol": "GRT", "usd": 500.0}]))
near_cap = dict(OPEN_BOOK, positions=[lot("BAT", 100000, 0.392, 0.3)])          # $39,200
check("...and against the $40K cost cap",
      ab.buy_gate("PYTH", NOON_CT, near_cap) == (True, "$500 lot")
      and ab.buy_gate("PYTH", NOON_CT, near_cap, pending=[{"symbol": "GRT", "usd": 500.0}])
      == (False, "crypto cost $39,700 is at the $40,000 cap"))
check("...and the same coin cannot be starred twice in a run",
      ab.buy_gate("GRT", NOON_CT, OPEN_BOOK, pending=[{"symbol": "GRT", "usd": 500.0}])
      == (False, "already starred this run"))
check("first_red cuts a red line to its headline",
      ab.first_red(["1st alert today", "LATE: already up 31% today - coins past +30% hit -15% "
                    "before +20% about 2 times in 3"]) == "LATE: already up 31% today"
      and ab.first_red(["small coin ($35M cap): runs more often"]) is None)
check("a hot week is information, not a red line (2026-10-16, weekly momentum research)",
      ab.first_red(["hot week: up 27% on the week (not a veto since 10-16: weekly momentum "
                    "research)"]) is None)
# The memecoin pack counts as one coin (2026-10-18): a second memecoin is an
# add to the first, and an add never gets the star.
shib = dict(OPEN_BOOK, positions=[lot("SHIB", 10_000_000, 0.00001, 0.0000085)])
check("a memecoin while another memecoin is held: no star, and it says which",
      ab.buy_gate("PEPE", NOON_CT, shib)
      == (False, "memecoin pack already held (SHIB): counts as one coin"), ab.buy_gate("PEPE", NOON_CT, shib))
check("a non-memecoin is unaffected", ab.buy_gate("GRT", NOON_CT, shib) == (True, "$500 lot"))
check("a memecoin on a book with no memecoin: a buy", ab.buy_gate("PEPE", NOON_CT, OPEN_BOOK) == (True, "$500 lot"))
check("a memecoin starred earlier in the same run counts as held",
      ab.buy_gate("PEPE", NOON_CT, OPEN_BOOK, pending=[{"symbol": "BONK", "usd": 500.0}])
      == (False, "memecoin pack already starred this run"))
check("the pack list has the sub-penny names the correlation study named",
      {"SHIB", "PEPE", "BONK", "FLOKI", "MEW", "PENGU"} <= ab.MEME_PACK)

# The crash budget (2026-10-16): the loss if every coin fell as far below its
# stop as on Oct 10, 2025, new lot included.
CG = {"PYTH": 0.54, "_default": 0.21}
check("crash case of one lot: cost minus the stop value after that coin's gap",
      abs(ab.crash_case([lot("PYTH", 1000, 1.0, 0.85)], CG) - (1000 - 850 * 0.46)) < 1e-9,
      ab.crash_case([lot("PYTH", 1000, 1.0, 0.85)], CG))
check("a new lot's crash loss: its 15% stop, then the default gap",
      abs(ab.crash_loss_new(500, "GRT", CG) - 500 * (1 - 0.85 * 0.79)) < 1e-9)
check("a coin's own gap beats the default, and a lot with no stop is taken at 15% under cost",
      ab.crash_gap(CG, "PYTH#4") == 0.54 and ab.crash_gap(CG, "GRT") == 0.21
      and ab.crash_gap({}, "GRT") == ab.DEFAULT_CRASH_GAP
      and abs(ab.crash_case([lot("GRT", 100, 1.0, None)], CG) - (100 - 85 * 0.79)) < 1e-9)
real_gaps = ab.load_crash_gaps
ab.load_crash_gaps = lambda: dict(CG)
one = dict(OPEN_BOOK, positions=[lot("PYTH", 1000, 1.0, 0.85)])        # crash case $609
check("inside the crash budget: a buy",
      ab.buy_gate("GRT", NOON_CT, dict(one, _crash_budget_usd=800)) == (True, "$500 lot"))
check("past it: no buy, with the numbers",
      ab.buy_gate("GRT", NOON_CT, dict(one, _crash_budget_usd=700))
      == (False, "over the crash budget ($773 vs $700)"),
      ab.buy_gate("GRT", NOON_CT, dict(one, _crash_budget_usd=700)))
check("a star earlier in the run counts against the crash budget too",
      ab.buy_gate("GRT", NOON_CT, dict(one, _crash_budget_usd=800),
                  pending=[{"symbol": "ZRX", "usd": 500.0}])[0] is False)
check("a half-size lot needs half the room",
      ab.buy_gate("GRT", NOON_CT, dict(one, _crash_budget_usd=700), half="high-vol coin")
      == (True, "$250 lot (half size: high-vol coin)"))
check("no budget set: no crash check",
      ab.buy_gate("GRT", NOON_CT, one) == (True, "$500 lot"))
bad = ab.buy_gate("GRT", NOON_CT, dict(one, _crash_budget_usd="lots"))
check("an unreadable crash budget fails closed: no buy, and it says why",
      bad[0] is False and bad[1].startswith("crash budget check failed"), bad)
ab.load_crash_gaps = real_gaps
real_file = ab.CRASH_GAPS_FILE
ab.CRASH_GAPS_FILE = ab.Path("/nonexistent/crash_gaps.json")
check("an unreadable gaps file means the default for every coin, not a crash",
      ab.load_crash_gaps() == {} and ab.crash_gap(ab.load_crash_gaps(), "PYTH") == ab.DEFAULT_CRASH_GAP)
ab.CRASH_GAPS_FILE = real_file
check("the no-catch-up line is on the red list too",
      ab.first_red(["no catch-up trade: BTC +6% over 3 days and this coin +0.3% - laggards "
                    "averaged -2.1%"]) == "no catch-up trade: BTC +6% over 3 days and this coin +0.3%")
check("the high-vol line halves the lot",
      ab.half_reason(["high-vol coin (6.4%/day): runs more often - half size"]) == "high-vol coin"
      and ab.half_reason(["small coin ($35M cap)"]) is None)

sent = []
ALL_PUSHES = []          # every push in this section, for the gold-star invariant at the end


def capture(title, message, **k):
    sent.append((title, message, k))
    ALL_PUSHES.append((title, message, k))


real_push, real_book = ab.push, ab.load_book
ab.push = capture
try:
    # The buy gate and the 7 AM digest are shared with BASE BREAKOUT. They are
    # exercised here through the rate path with the old TOP SETUP routing
    # switched back on; the default (TOP SETUP alert only) is checked after.
    ab.TOP_SETUP_IS_BUY = True
    grt = {"kind": "crypto", "symbol": "GRT", "name": "The Graph", "price": 0.0295, "pct": 6.3,
           "week_pct": 8.0, "low_24h": 0.0271, "rv_source": "coinbase"}
    ab.load_book = lambda: unlocked
    pyth = dict(grt, symbol="PYTH", name="Pyth Network", price=0.0958)
    ab.send_rate([("rate:crypto:PYTH", pyth, 3.9, 71.0, 6.2)], {}, NOON_CT, False,
                 state={"alerts_today": {}, "streak": {}})
    title, body, kw = sent[-1]
    check("a buy signal on a held, unlocked coin: no star, quiet, the reason in the title",
          kw.get("quiet") is True and kw.get("priority") == "low" and kw.get("tags") == "no_entry_sign"
          and title.startswith("PYTH - not a buy - ")
          and title.endswith("(held, first lot not locked: no add)")
          and body.startswith("NOT A BUY: held, first lot not locked"), (title, body, kw))
    ab.load_book = lambda: full
    sent.clear()
    ab.send_rate([("rate:crypto:GRT", dict(grt), 3.9, 71.0, 6.2)], {}, NOON_CT, False,
                 state={"alerts_today": {}, "streak": {}})
    title, body, kw = sent[-1]
    check("ten coins held: no star, quiet, with the reason",
          kw.get("quiet") is True and kw.get("tags") == "no_entry_sign"
          and title.endswith("(10 coins held, max 10)"), (title, kw))
    ab.load_book = lambda: OPEN_BOOK
    sent.clear()
    hot = dict(grt, week_pct=25.0)
    ab.send_rate([("rate:crypto:GRT", hot, 3.9, 71.0, 6.2)], {}, NOON_CT, False,
                 state={"alerts_today": {}, "streak": {}})
    title, body, kw = sent[-1]
    check("a hot week no longer takes the star (old TOP SETUP routing switched on)",
          kw.get("tags") == "star" and kw.get("quiet") is False
          and "hot week: up 25% on the week" in body and title.startswith("GRT - BUY"), (title, kw))
    late = dict(grt, pct=31.0)
    sent.clear()
    ab.send_rate([("rate:crypto:GRT", late, 3.9, 71.0, 6.2)], {}, NOON_CT, False,
                 state={"alerts_today": {}, "streak": {}})
    title, body, kw = sent[-1]
    check("a LATE line still takes it",
          kw.get("tags") != "star" and kw.get("quiet") is True, (title, kw))
    sent.clear()
    st = {"alerts_today": {}, "streak": {}}
    ab.send_rate([("rate:crypto:GRT", dict(grt), 3.9, 71.0, 6.2)], {}, NINE_PM, False, state=st)
    title, body, kw = sent[-1]
    check("a buy signal at 9 PM: the hourglass, quiet, kept for the morning",
          kw.get("quiet") is True and kw.get("tags") == "hourglass_flowing_sand"
          and title.startswith("GRT - not a buy yet - ")
          and [s["symbol"] for s in st.get("night_stars", [])] == ["GRT"]
          and st["night_stars"][0].get("kind") == "top", (title, st.get("night_stars")))
    sent.clear()
    ab.send_night_digest(st, [dict(grt, price=0.0310)], SEVEN_AM)
    title, body, kw = sent[-1] if sent else ("", "", {})
    check("7 AM: a TOP SETUP kept overnight is no base breakout now: no star, quiet",
          len(sent) == 1 and kw.get("quiet") is True and kw.get("tags") == "no_entry_sign"
          and title == "Overnight setups, not a buy now: GRT" and "not a base breakout now" in body
          and "+5.1%" in body, (title, body, kw))
    check("...and the list is cleared", st["night_stars"] == [])
    sent.clear()
    ab.send_night_digest(st, [], SEVEN_AM + 300)
    check("no signals, no digest", sent == [])
    st = {"night_stars": [{"symbol": "GRT", "what": "base breakout", "price": 0.0295,
                           "ts": NINE_PM - 20 * 3600}]}
    ab.send_night_digest(st, [], SEVEN_AM)
    check("a signal older than 14 hours is dropped, not sent", sent == [] and st["night_stars"] == [])
    sent.clear()
    ab.send_alerts([(15.0, "crypto:GRT", dict(grt, pct=16.0, dollars=5e7))], {}, ab.CRYPTO_HIGH_PRIORITY_LEVEL,
                   False, state=None, now_ts=NOON_CT)
    check("a threshold crossing is a mover: quiet feed",
          sent and sent[-1][2].get("quiet") is True and sent[-1][2].get("priority") == "low", sent[-1:])
    check("...and it is marked as a mover push, so MOVER_PUSHES drops it (2026-10-19)",
          sent[-1][2].get("mover") is True, sent[-1:])
    sent.clear()
    ab.send_dip({"symbol": "GRT", "why": "test"}, {"off_high": 12.0, "high": 0.034, "off_low": 4.0, "low": 0.0288}, 0.03)
    check("the dip push is marked as a mover push", sent and sent[-1][2].get("mover") is True, sent[-1:])
    sent.clear()
    _tsb = ab.TOP_SETUP_IS_BUY
    ab.TOP_SETUP_IS_BUY = False
    try:
        ab.send_rate([("rate:crypto:GRT", dict(grt), 3.9, 71.0, 6.2)], {}, NOON_CT, False, state={"alerts_today": {}, "streak": {}})
    finally:
        ab.TOP_SETUP_IS_BUY = _tsb
    check("a quiet rate alert is marked as a mover push", sent and sent[-1][2].get("mover") is True, sent[-1:])
    sent.clear()
    ab.send_rate([("rate:crypto:GRT", dict(grt), 3.9, 71.0, 6.2)], {}, NOON_CT, False, state={"alerts_today": {}, "streak": {}})
    check("a starred rate alert (TOP SETUP on) is not a mover push", sent and sent[-1][2].get("mover") is False, sent[-1:])

    # The gold star end to end (2026-10-16). SEVEN_AM above is a Saturday, when
    # no base breakout is green, so these run Monday night to Tuesday morning.
    MON_9PM = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc).timestamp()     # 9 PM CDT Mon
    TUE_7AM = datetime(2026, 10, 20, 12, 5, tzinfo=timezone.utc).timestamp()    # 7:05 AM CDT Tue
    TUE_NOON = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc).timestamp()
    uday = ab.dc.utc_day(TUE_7AM)
    entry = {"d": uday, "prev_close": 0.0280, "base14": 9.0, "avg50": 0.0270,
             "high30": 0.0300, "close3": 0.0279, "vol30": 3.0, "days": [0.5, -0.4, 0.3]}
    base_ctx = ab.dc.coin_context(entry, 0.0295, uday)                          # +5.4% today

    def gstate(**extra):
        # The cached Bitstamp list keeps the depth line off the network.
        return dict({"alerts_today": {}, "streak": {},
                     "daily": {"GRT": dict(entry),
                               "_bitstamp": {"d": uday, "pairs": {"BTC": "btcusd"}}}}, **extra)

    ab.load_book = lambda: OPEN_BOOK
    sent.clear()
    st = gstate()
    ab.send_breakouts([("base:crypto:GRT", dict(grt), base_ctx)], {}, TUE_NOON, False, state=st)
    title, body, kw = sent[-1]
    check("noon BASE BREAKOUT, room in the book: the gold star, loud, max priority, BUY in the title",
          kw.get("tags") == "star" and kw.get("quiet") is False and kw.get("priority") == "max"
          and title == "GRT - BUY $500 lot - base breakout +5.4% today"
          and body.startswith("BUY NOW: $500 lot, then the 15% stop"), (title, body, kw))
    check("...and it counts against the book for the rest of the run",
          st.get("run_stars") == [{"symbol": "GRT", "usd": 500.0}], st.get("run_stars"))
    check("...and is remembered for the still-a-buy reminders (2026-10-21)",
          st.get("open_stars") == [{"symbol": "GRT", "ts": TUE_NOON, "usd": 500.0,
                                    "what": "base breakout +5.4% today", "repeats": 0}],
          st.get("open_stars"))
    star_state = st
    ab.load_book = lambda: dict(OPEN_BOOK, positions=[lot("GRT", 17000, 0.0250, 0.0262)])
    sent.clear()
    ab.send_breakouts([("base:crypto:GRT", dict(grt), base_ctx)], {}, TUE_NOON, False, state=gstate())
    title, body, kw = sent[-1]
    check("held and locked: no star, adds come only from a calm stop check",
          kw.get("tags") == "no_entry_sign" and kw.get("quiet") is True
          and title == "GRT - not a buy - base breakout +5.4% today (held: adds only at a calm stop check)",
          (title, kw))
    ab.load_book = lambda: OPEN_BOOK
    sent.clear()
    st = gstate()
    st["daily"]["GRT"]["vol30"] = 6.4
    ab.send_breakouts([("base:crypto:GRT", dict(grt), base_ctx)], {}, TUE_NOON, False, state=st)
    title, body, kw = sent[-1]
    check("a high-vol coin: the gold star for half a lot",
          kw.get("tags") == "star"
          and title == "GRT - BUY $250 lot (half size: high-vol coin) - base breakout +5.4% today"
          and st.get("run_stars") == [{"symbol": "GRT", "usd": 250.0}], (title, st.get("run_stars")))
    ab.load_book = lambda: OPEN_BOOK
    sent.clear()
    st = gstate()
    ab.send_breakouts([("base:crypto:GRT", dict(grt, week_pct=27.0), base_ctx)], {}, TUE_NOON,
                      False, state=st)
    title, body, kw = sent[-1]
    check("a hot-week BASE BREAKOUT gets the gold star (not a veto since 10-16)",
          kw.get("tags") == "star" and title == "GRT - BUY $500 lot - base breakout +5.4% today"
          and "hot week: up 27% on the week" in body, (title, kw))
    real_gaps = ab.load_crash_gaps
    ab.load_crash_gaps = lambda: {"PYTH": 0.54, "_default": 0.21}
    ab.load_book = lambda: dict(OPEN_BOOK, _crash_budget_usd=700,
                                positions=[lot("PYTH", 1000, 1.0, 0.85)])
    sent.clear()
    ab.send_breakouts([("base:crypto:GRT", dict(grt), base_ctx)], {}, TUE_NOON, False,
                      state=gstate())
    title, body, kw = sent[-1]
    check("a breakout past the crash budget: no star, quiet, the numbers in the title",
          kw.get("tags") == "no_entry_sign" and kw.get("quiet") is True
          and title.endswith("(over the crash budget ($773 vs $700))")
          and body.startswith("NOT A BUY: over the crash budget"), (title, kw))
    ab.load_crash_gaps = real_gaps
    ab.load_book = lambda: OPEN_BOOK
    sent.clear()
    st = gstate()
    ab.send_breakouts([("base:crypto:GRT", dict(grt), base_ctx)], {}, MON_9PM, False, state=st)
    title, body, kw = sent[-1]
    check("9 PM BASE BREAKOUT: the hourglass, quiet, kept for 7 AM",
          kw.get("tags") == "hourglass_flowing_sand" and kw.get("quiet") is True
          and title == "GRT - not a buy yet - base breakout +5.4% today (7 AM re-check)"
          and body.startswith("NOT A BUY YET")
          and [s.get("kind") for s in st.get("night_stars", [])] == ["base"],
          (title, kw, st.get("night_stars")))
    sent.clear()
    ab.send_night_digest(st, [dict(grt, price=0.0300)], TUE_7AM)
    title, body, kw = sent[-1] if sent else ("", "", {})
    check("7 AM, still a base breakout and room in the book: its own gold star, loud, max priority",
          len(sent) == 1 and kw.get("tags") == "star" and kw.get("quiet") is False
          and kw.get("priority") == "max" and kw.get("click") == "https://robinhood.com/crypto/GRT"
          and title == "GRT - BUY $500 lot - overnight breakout, still a buy"
          and body.startswith("BUY NOW: $500 lot") and "+1.7%" in body, (title, body, kw))
    check("...the list is cleared and the star counts for the run",
          st["night_stars"] == [] and st.get("run_stars") == [{"symbol": "GRT", "usd": 500.0}],
          (st["night_stars"], st.get("run_stars")))
    check("...and the 7 AM star is remembered for the reminders too",
          [(s["symbol"], s["ts"], s["what"], s["repeats"]) for s in st.get("open_stars", [])]
          == [("GRT", TUE_7AM, "overnight breakout", 0)], st.get("open_stars"))
    night = [{"symbol": "GRT", "what": "base breakout +5.4% today", "kind": "base",
              "price": 0.0295, "ts": MON_9PM}]
    sent.clear()
    ab.send_night_digest(gstate(night_stars=list(night)), [dict(grt, price=0.0287)], TUE_7AM)
    title, body, kw = sent[-1] if sent else ("", "", {})
    check("7 AM, faded under +5% on the day: no star, quiet, the reason listed",
          len(sent) == 1 and kw.get("quiet") is True and kw.get("tags") == "no_entry_sign"
          and "BUY" not in title and "not a base breakout now (no +5% day)" in body, (title, body, kw))
    sent.clear()
    st = gstate(night_stars=list(night))
    st["daily"]["BTC"] = {"d": uday, "prev_close": 90000.0, "base14": 5.0, "avg50": 88000.0,
                          "high30": 92000.0, "close3": 89000.0, "vol30": 2.0,
                          "days": [0.4, -0.2, 0.1]}
    ab.send_night_digest(st, [dict(grt, price=0.0300)], TUE_7AM, btc_price=85500.0)
    title, body, kw = sent[-1] if sent else ("", "", {})
    check("7 AM, still a breakout but BTC -5% on the day: a red line, no star",
          len(sent) == 1 and kw.get("tags") == "no_entry_sign"
          and "RED: BTC -5.0% in 24h" in body, (title, body))
    ab.load_book = lambda: full
    sent.clear()
    ab.send_night_digest(gstate(night_stars=list(night)), [dict(grt, price=0.0300)], TUE_7AM)
    check("7 AM with the book full: no star, quiet, the reason listed",
          sent and sent[-1][2].get("quiet") is True and sent[-1][2].get("tags") == "no_entry_sign"
          and "10 coins held, max 10" in sent[-1][1], sent[-1:])
    ab.load_book = lambda: nine
    sent.clear()
    st = gstate(night_stars=list(night) + [dict(night[0], symbol="ZRX")])
    st["daily"]["ZRX"] = dict(entry)
    ab.send_night_digest(st, [dict(grt, price=0.0300),
                              dict(grt, symbol="ZRX", name="0x", price=0.0300)], TUE_7AM)
    stars = [s for s in sent if s[2].get("tags") == "star"]
    check("two still green, room for one lot: one gold star, the other listed with the reason",
          len(stars) == 1 and stars[0][0].startswith("GRT - BUY")
          and any("ZRX" in s[0] and "9 coins held + 1 starred this run, max 10" in s[1]
                  for s in sent if s[2].get("quiet")), sent)
    ab.load_book = lambda: OPEN_BOOK
    st = gstate(night_stars=list(night))
    ab.push = lambda title, message, **k: False
    ab.send_night_digest(st, [dict(grt, price=0.0300)], TUE_7AM)
    ab.push = capture
    check("a gold star that fails to send is kept for the next run and not counted",
          [s["symbol"] for s in st["night_stars"]] == ["GRT"] and not st.get("run_stars"), st)
    sent.clear()

    # STILL A BUY reminders (2026-10-21): a star the phone may have dropped is
    # re-sent at 10 and 30 minutes while the coin is still a buy and not held.
    MIN = 60.0
    ab.load_book = lambda: OPEN_BOOK
    st = dict(star_state, run_stars=[], open_stars=[dict(s) for s in star_state["open_stars"]])
    ab.send_star_repeats(st, [dict(grt)], TUE_NOON + 5 * MIN)
    check("five minutes after the star: nothing yet, the star is kept",
          sent == [] and [s["repeats"] for s in st["open_stars"]] == [0], (sent, st["open_stars"]))
    ab.send_star_repeats(st, [dict(grt)], TUE_NOON + 10 * MIN)
    title, body, kw = sent[-1] if sent else ("", "", {})
    check("ten minutes after: STILL A BUY, loud, max priority, the star tag, tap-through",
          len(sent) == 1 and kw.get("tags") == "star" and kw.get("quiet") is False
          and kw.get("priority") == "max" and kw.get("click") == "https://robinhood.com/crypto/GRT"
          and title == "GRT - BUY $500 lot - still a buy (base breakout +5.4% today), star sent 12:00 PM CT"
          and body.startswith("STILL A BUY: the gold star for GRT went out at 12:00 PM CT and GRT is "
                              "not in positions.json. BUY NOW: $500 lot")
          and "re-checked now: still a base breakout (+5.4% today), no red line" in body
          and body.endswith("One more reminder at 30 minutes if it is still a buy."), (title, body, kw))
    check("...counted once, kept for the second reminder, not counted as a new star",
          [s["repeats"] for s in st["open_stars"]] == [1] and st.get("run_stars") == [],
          (st["open_stars"], st.get("run_stars")))
    sent.clear()
    ab.send_star_repeats(st, [dict(grt)], TUE_NOON + 20 * MIN)
    check("twenty minutes: nothing (the second reminder is at thirty)", sent == [], sent)
    ab.send_star_repeats(st, [dict(grt)], TUE_NOON + 30 * MIN)
    title, body, kw = sent[-1] if sent else ("", "", {})
    check("thirty minutes: the last reminder, then the star is forgotten",
          len(sent) == 1 and title.startswith("GRT - BUY $500 lot - still a buy")
          and body.endswith("Last reminder.") and st["open_stars"] == [], (title, body, st["open_stars"]))
    sent.clear()
    ab.send_star_repeats(st, [dict(grt)], TUE_NOON + 40 * MIN)
    check("...and nothing after that", sent == [], sent)
    st = dict(star_state, run_stars=[], open_stars=[dict(s) for s in star_state["open_stars"]])
    ab.load_book = lambda: dict(OPEN_BOOK, positions=[lot("GRT", 17000, 0.0295, None, bought="2026-10-20")])
    ab.send_star_repeats(st, [dict(grt)], TUE_NOON + 10 * MIN)
    check("bought (GRT is in positions.json): no reminder, the star is forgotten",
          sent == [] and st["open_stars"] == [], (sent, st["open_stars"]))
    ab.load_book = lambda: OPEN_BOOK
    st = dict(star_state, run_stars=[], open_stars=[dict(s) for s in star_state["open_stars"]])
    ab.send_star_repeats(st, [dict(grt, price=0.0285)], TUE_NOON + 10 * MIN)      # +1.8% today
    check("no longer a breakout (faded under +5%): no reminder, the star is let go",
          sent == [] and st["open_stars"] == [], (sent, st["open_stars"]))
    st = dict(star_state, run_stars=[], open_stars=[dict(s) for s in star_state["open_stars"]])
    ab.send_star_repeats(st, [dict(grt)], TUE_NOON + 10 * MIN, btc_price=85500.0)
    st2 = dict(star_state, run_stars=[], open_stars=[dict(s) for s in star_state["open_stars"]])
    st2["daily"]["BTC"] = {"d": uday, "prev_close": 90000.0, "base14": 5.0, "avg50": 88000.0,
                           "high30": 92000.0, "close3": 89000.0, "vol30": 2.0, "days": [0.4, -0.2, 0.1]}
    sent.clear()
    ab.send_star_repeats(st2, [dict(grt)], TUE_NOON + 10 * MIN, btc_price=85500.0)
    check("a red line since the star (BTC -5% on the day): no reminder, the star is let go",
          sent == [] and st2["open_stars"] == [], (sent, st2["open_stars"]))
    st = dict(star_state, run_stars=[], open_stars=[dict(s) for s in star_state["open_stars"]])
    TUE_8PM = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc).timestamp()         # 8 PM CDT Tue
    ab.send_star_repeats(st, [dict(grt)], TUE_8PM)
    check("after 7 PM: no reminder and nothing carried into the evening",
          sent == [] and st["open_stars"] == [], (sent, st["open_stars"]))
    st = dict(star_state, run_stars=[], open_stars=[dict(s) for s in star_state["open_stars"]])
    ab.push = lambda title, message, **k: False
    ab.send_star_repeats(st, [dict(grt)], TUE_NOON + 10 * MIN)
    ab.push = capture
    check("a reminder that fails to send is tried again next run",
          [s["repeats"] for s in st["open_stars"]] == [0], st["open_stars"])
    sent.clear()
    # The default since 2026-10-16: a TOP SETUP is an alert only.
    ab.TOP_SETUP_IS_BUY = False
    ab.load_book = lambda: OPEN_BOOK
    sent.clear()
    st = {"alerts_today": {}, "streak": {}}
    ab.send_rate([("rate:crypto:GRT", dict(grt), 3.9, 71.0, 6.2)], {}, NOON_CT, False, state=st)
    title, body, kw = sent[-1]
    check("default: a TOP SETUP at noon with room in the book stays quiet, no star",
          not title.startswith("*") and kw.get("quiet") is True and kw.get("priority") == "low"
          and "not a buy signal" in body, (title, kw))
    sent.clear()
    st = {"alerts_today": {}, "streak": {}}
    ab.send_rate([("rate:crypto:GRT", dict(grt), 3.9, 71.0, 6.2)], {}, NINE_PM, False, state=st)
    check("default: a 9 PM TOP SETUP is not kept for the 7 AM digest",
          sent and not st.get("night_stars"), st.get("night_stars"))
    stars = [p for p in ALL_PUSHES if p[2].get("tags") == "star"]
    check("the gold star is only ever on a loud BUY, and every BUY has it",
          stars and all(" - BUY " in p[0] and p[2].get("quiet") is False for p in stars)
          and all(" - BUY " not in p[0] for p in ALL_PUSHES if p[2].get("tags") != "star"),
          [(p[0], p[2].get("tags"), p[2].get("quiet")) for p in ALL_PUSHES])
finally:
    ab.push, ab.load_book = real_push, real_book
    ab.TOP_SETUP_IS_BUY = False

print("\nStop watch: held coins under a stop's sell limit")
sent = []
real_push = ab.push
ab.push = lambda title, message, **k: sent.append((title, message, k))
try:
    book = {"positions": [lot("PYTH", 5500, 0.0899, 0.0756), lot("PYTH#2", 4500, 0.0836, 0.0703),
                          lot("XCN", 250000, 0.0046, 0.00387), lot("AERO", 1000, 0.934, 0.794),
                          lot("UNI", 150, 7.14, 6.171)]}
    prices = {"PYTH-USD": 0.0700, "XCN-USD": 0.00380, "AERO-USD": 0.790, "UNI-USD": 7.40}
    st = {}
    ab.check_live_stops(st, NOON_CT, book=book, price_of=lambda p: prices[p])
    titles = [t for t, _, _ in sent]
    check("PYTH 7.4% under its 0.0756 stop -> CHECK STOPS at max",
          "CHECK STOPS: PYTH under its stop limit" in titles
          and all(k.get("priority") == "max" for _, _, k in sent), titles)
    pyth = next(m for t, m, _ in sent if t.startswith("CHECK STOPS: PYTH"))
    check("...naming the lot whose limit is crossed, not the one still above it",
          "PYTH: stop $0.0756" in pyth and "PYTH#2" not in pyth, pyth)
    check("three coins at or under their stops -> one CRASH CHECK",
          sum(t.startswith("CRASH CHECK: 3 coins") for t in titles) == 1, titles)
    check("XCN and AERO under the stop but inside the 5% limit -> no CHECK STOPS",
          not any(t.startswith("CHECK STOPS: XCN") or t.startswith("CHECK STOPS: AERO") for t in titles), titles)
    sent.clear()
    ab.check_live_stops(st, NOON_CT + 3600, book=book, price_of=lambda p: prices[p])
    check("an hour later: no repeat", sent == [], sent)
    ab.check_live_stops(st, NOON_CT + 7 * 3600, book=book, price_of=lambda p: prices[p])
    check("seven hours later: it says so again", any(t.startswith("CHECK STOPS: PYTH") for t, _, _ in sent))
    sent.clear()
    def down(p):
        raise OSError("coinbase down")
    ab.check_live_stops({}, NOON_CT, book=book, price_of=down)
    check("no prices: nothing sent, no crash", sent == [])
finally:
    ab.push = real_push

print("\nPushes: two tries, failures counted, quiet topic")
import urllib.request as _ur
import time as _time
calls = []


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


real_open, real_sleep = _ur.urlopen, _time.sleep
real_topic, real_quiet = ab.NTFY_TOPIC, ab.NTFY_TOPIC_QUIET
try:
    ab.NTFY_TOPIC, ab.NTFY_TOPIC_QUIET = "main-topic", "main-topic-quiet"
    _time.sleep = lambda s: None
    _ur.urlopen = lambda req, timeout=None: (calls.append((req.full_url, req.headers)), _Resp(200))[1]
    check("a quiet push goes to the quiet topic at low priority",
          ab.push("t", "m", priority="high", quiet=True) is True
          and calls[-1][0].endswith("/main-topic-quiet") and calls[-1][1].get("Priority") == "low", calls[-1:])
    check("a loud push goes to the main topic", ab.push("t", "m", loud=True) is True and calls[-1][0].endswith("/main-topic"))
    check("with LOUD_ONLY, an ordinary push goes to the quiet topic (2026-10-19)",
          ab.LOUD_ONLY is False or (ab.push("t", "m") is True and calls[-1][0].endswith("/main-topic-quiet")))
    check("with LOUD_ONLY, a gold-star BUY still goes to the main topic (2026-10-19)",
          ab.push("t", "m", tags=ab.BUY_TAG) is True and calls[-1][0].endswith("/main-topic"))
    # Mover pushes off (2026-10-19): dropped before any network call, True returned.
    check("MOVER_PUSHES ships off", ab.MOVER_PUSHES is False)
    n_before = len(calls)
    check("a mover push is dropped, not sent, and reads as sent",
          ab.push("t", "m", quiet=True, mover=True) is True and len(calls) == n_before, calls[n_before:])
    check("a loud mover push is dropped too",
          ab.push("t", "m", priority="max", mover=True) is True and len(calls) == n_before)
    check("a non-mover push still goes out", ab.push("t", "m", quiet=True) is True and len(calls) == n_before + 1)
    check("the gold star is never a mover", ab.push("t", "m", tags=ab.BUY_TAG) is True and len(calls) == n_before + 2)
    saved_mp = ab.MOVER_PUSHES
    ab.MOVER_PUSHES = True
    try:
        check("MOVER_PUSHES True restores the mover pushes (quiet feed under LOUD_ONLY)",
              ab.push("t", "m", quiet=True, mover=True) is True and len(calls) == n_before + 3
              and calls[-1][0].endswith("/main-topic-quiet"))
    finally:
        ab.MOVER_PUSHES = saved_mp
    before = ab.PUSH_STATS["failed"]
    calls.clear()

    def boom(req, timeout=None):
        calls.append(req.full_url)
        raise OSError("down")

    _ur.urlopen = boom
    check("both tries fail -> False", ab.push("t", "m") is False and len(calls) == 2, calls)
    check("...and the failure is counted for the exit code", ab.PUSH_STATS["failed"] == before + 1)
    ab.PUSH_STATS["failed"] = before
finally:
    _ur.urlopen, _time.sleep = real_open, real_sleep
    ab.NTFY_TOPIC, ab.NTFY_TOPIC_QUIET = real_topic, real_quiet

# What a gold-star lot loses if its stop fills (2026-10-17).
check("a $500 lot loses about $87 if its stop fills", abs(ab.stop_loss_usd(500) - 87.09) < 0.05,
      ab.stop_loss_usd(500))
check("...a $250 half lot about $44", round(ab.stop_loss_usd(250)) == 44, ab.stop_loss_usd(250))
check("the buy line says what the stop costs, then asks for the Robinhood alert (2026-10-18)",
      ab.buy_head("$500 lot", 500.0) == "BUY NOW: $500 lot, then the 15% stop below as soon as it fills."
      " If that stop fills: about -$87 of the $500. Then a Robinhood price alert at that stop.",
      ab.buy_head("$500 lot", 500.0))
check("no size, no loss line", ab.buy_head("x", 0) == "BUY NOW: x, then the 15% stop below as soon as it fills."
      " Then a Robinhood price alert at that stop.", ab.buy_head("x", 0))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all passed")
