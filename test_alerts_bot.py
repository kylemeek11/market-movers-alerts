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
check("a hot week gets the caution even on a top setup", top and any(l.startswith("caution") for l in lines), lines)
top, lines = ab.tier_lines(crow(week_pct=19.9), 2.6, False)
check("just under the week bar: no caution", lines == [], lines)
top, lines = ab.tier_lines(crow(week_pct=None), 2.6, False)
check("no week figure: no caution, no crash", lines == [], lines)
top, lines = ab.tier_lines({"kind": "stock", "symbol": "S", "pct": 3.0, "week_pct": 30.0}, 6.0, True)
check("stocks get the caution but never the star", not top and len(lines) == 1, lines)

# The whole thing through send_rate: title, tag, priority.
sent = []
real_push = ab.push
ab.push = lambda title, message, **k: sent.append((title, message, k.get("priority"), k.get("tags")))
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
    ab.push = real_push
t1, b1, p1, g1 = sent[0]
check("first GRT alert is a starred top setup at high priority", t1.startswith("* GRT") and p1 == "high" and g1 == "star", (t1, p1, g1))
check("...and the body says so and quotes a 15% stop", "TOP SETUP" in b1 and "15% stop" in b1, b1)
t2, b2, p2, g2 = sent[1]
check("the repeat an hour later is unstarred and quieter", not t2.startswith("*") and p2 == "default" and "alert #2 today" in b2, (t2, p2, b2))
t3, b3, p3, g3 = sent[2]
check("Quant up 130% on the week carries the caution, no star", "caution: already up 130% on the week" in b3 and not t3.startswith("*") and p3 == "high", (t3, p3, b3))
t4, b4, p4, g4 = sent[3]
check("overnight still goes out at max priority", p4 == "max", p4)

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

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all passed")
