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


def replay_near(stamp_from=None):
    """Run the real record_marks/collect_rate over that morning.

    stamp_from limits when NEAR enters the tracked set, which is how the old
    code behaved - it only stamped coins that had already moved.
    """
    marks, fired = {}, {}
    for hhmm in RUN_TIMES:
        now = ct(hhmm)
        row = {"kind": "crypto", "symbol": "NEAR", "name": "NEAR Protocol",
               "price": near_price_at(hhmm), "pct": 5.0,
               "dollars": 1.6e9, "hour_pct": None}
        if stamp_from is None or ct(hhmm) >= ct(stamp_from):
            ab.record_marks([row], marks, now)
        hits = ab.collect_rate([row], marks, fired, now,
                               ab.CRYPTO_RATE_PCT, ab.CRYPTO_SLOW_RATE_PCT)
        if hits:
            fkey, r, move, elapsed = hits[0]
            fired[fkey] = now
            return hhmm, r["price"], move, elapsed
    return None


print("NEAR replay - the run this was built to catch")
hit = replay_near()
check("fires before the 15% day alert at 11:36",
      hit is not None and ct(hit[0]) < ct("11:36"), f"got {hit}")
check("fires at a price below $4.05",
      hit is not None and hit[1] < 4.05, f"got {hit}")
if hit:
    print(f"       -> {hit[0]} CT  ${hit[1]:.4f}  "
          f"+{hit[2]:.1f}% over {hit[3]:.0f} min  "
          f"(peak was $4.28; the alert you got was 11:36 @ $4.23)")

print("\nThe bug this fixes")
old = replay_near(stamp_from="11:02")
check("old behaviour (stamps only once already moving) misses it",
      old is None or ct(old[0]) >= ct("11:36"), f"got {old}")

print("\nTrack separation")
marks, fired = {}, {}
spike = {"kind": "crypto", "symbol": "SPK", "name": "Spike", "price": 100.0,
         "pct": 1.0, "dollars": 5e7, "hour_pct": None}
t0 = ct("10:00")
ab.record_marks([spike], marks, t0)
spike["price"] = 105.5
hits = ab.collect_rate([spike], marks, fired, t0 + 10 * 60,
                       ab.CRYPTO_RATE_PCT, ab.CRYPTO_SLOW_RATE_PCT)
check("fast track catches +5.5% in 10 min", len(hits) == 1)
check("fast track reports ~10 min", hits and 9 <= hits[0][3] <= 11)

marks, fired = {}, {}
creep = {"kind": "crypto", "symbol": "CRP", "name": "Creep", "price": 100.0,
         "pct": 1.0, "dollars": 5e7, "hour_pct": None}
for i in range(0, 46, 8):
    creep["price"] = 100.0 * (1 + 0.0015 * i)   # ~6.8% over 45 min, never 5% in 20
    ab.record_marks([creep], marks, t0 + i * 60)
hits = ab.collect_rate([creep], marks, fired, t0 + 45 * 60,
                       ab.CRYPTO_RATE_PCT, ab.CRYPTO_SLOW_RATE_PCT)
check("slow track catches a steady climb the fast track cannot", len(hits) == 1)
check("slow track reports a 20min+ window", hits and hits[0][3] >= 20)

print("\nCooldown and breadth")
marks, fired = {}, {}
ab.record_marks([spike], marks, t0)
spike["price"] = 120.0
h1 = ab.collect_rate([spike], marks, fired, t0 + 10 * 60,
                     ab.CRYPTO_RATE_PCT, ab.CRYPTO_SLOW_RATE_PCT)
fired[h1[0][0]] = t0 + 10 * 60
ab.record_marks([spike], marks, t0 + 10 * 60)
h2 = ab.collect_rate([spike], marks, fired, t0 + 20 * 60,
                     ab.CRYPTO_RATE_PCT, ab.CRYPTO_SLOW_RATE_PCT)
check("same name does not re-alert inside the cooldown", len(h2) == 0)
# Keep it climbing and keep stamping, the way a live run would.
for i in (20, 30, 40, 50):
    spike["price"] = 120.0 + i
    ab.record_marks([spike], marks, t0 + i * 60)
spike["price"] = 200.0
h3 = ab.collect_rate([spike], marks, fired, t0 + 56 * 60,
                     ab.CRYPTO_RATE_PCT, ab.CRYPTO_SLOW_RATE_PCT)
check("it can alert again after the cooldown", len(h3) == 1)

universe = [{"symbol": f"C{i}"} for i in range(100)]
check("a market-wide lift is suppressed", ab.too_broad(list(range(40)), universe))
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
check("stablecoins are excluded", "USDT" not in syms)
check("illiquid coins are excluded", "TINY" not in syms)
check("non-ticker symbols are excluded", "龙虾" not in syms and len(syms) == 2,
      sorted(syms))
check("a null change percent does not crash", "NULL" not in syms)

print("\nStale marks")
marks = {"crypto:OLD": [[t0 - 3 * 3600, 1.0]], "crypto:NEW": [[t0, 1.0]]}
ab.drop_stale_marks(marks, t0)
check("stale names are forgotten", list(marks) == ["crypto:NEW"])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all passed")
