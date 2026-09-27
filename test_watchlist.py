#!/usr/bin/env python3
"""Tests for the watchlist signals. Run: python3 test_watchlist.py (no network)."""

import json
import sys
import tempfile

import alerts_bot as ab
import watchlist as wl

# These suites test the price signal and the CoinGecko arithmetic; keep the
# Coinbase confirmation out of the way so nothing here touches the network.
ab.coinbase_relvol = lambda *a, **k: None

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {'' if cond else detail}")
    if not cond:
        FAILURES.append(name)


def candles(highs_lows):
    """[(high, low), ...] oldest first -> Coinbase-shaped rows."""
    return [[i * 3600, lo, hi, lo, lo, 1.0] for i, (hi, lo) in enumerate(highs_lows)]


# A week near 100, then a slide to a 12-hour low of 90.
week = [(100.0, 99.0)] * 156 + [(95.0, 90.0)] * 12

print("dip_state")
st = wl.dip_state(candles(week), 91.9, 8.0, 2.0)
check("8.1% below the 100 high", round(st["off_high"], 1) == 8.1, st)
check("2.1% off the 90 low", round(st["off_low"], 1) == 2.1, st)
check("in zone and turning", st["in_zone"] and st["turning"], st)

st = wl.dip_state(candles(week), 90.5, 8.0, 2.0)
check("still at the low: in zone, not turning",
      st["in_zone"] and not st["turning"], st)

st = wl.dip_state(candles(week), 97.0, 8.0, 2.0)
check("shallow pullback: not in zone", not st["in_zone"], st)

st = wl.dip_state(candles(week), 105.0, 8.0, 2.0)
check("live price above the candle high becomes the high",
      st["high"] == 105.0 and st["off_high"] == 0.0, st)

check("too little history -> None", wl.dip_state(candles(week[:5]), 92.0, 8, 2) is None)

print("check_dips")
coin = {"symbol": "LINK", "product": "LINK-USD", "dip_pct": 8.0,
        "turn_pct": 2.0, "why": "test"}
sent = []
fired = {}
n = wl.check_dips([coin], {"LINK": 91.9}, fired, 1_000_000, False,
                  lambda c, s, p: sent.append((c["symbol"], p)),
                  fetch=lambda p: candles(week), log=lambda m: None)
check("turning dip is sent once", n == 1 and sent == [("LINK", 91.9)], sent)
check("and recorded", "dip:crypto:LINK" in fired, fired)

n = wl.check_dips([coin], {"LINK": 91.9}, fired, 1_000_000 + 3600, False,
                  lambda c, s, p: sent.append(1),
                  fetch=lambda p: candles(week), log=lambda m: None)
check("not repeated within 24h", n == 0 and len(sent) == 1)

n = wl.check_dips([coin], {"LINK": 91.9}, fired, 1_000_000 + 25 * 3600, False,
                  lambda c, s, p: sent.append(1),
                  fetch=lambda p: candles(week), log=lambda m: None)
check("sent again after 24h", n == 1)

fired = {}
n = wl.check_dips([coin], {"LINK": 91.9}, fired, 1_000_000, True,
                  lambda c, s, p: sent.append(1),
                  fetch=lambda p: candles(week), log=lambda m: None)
check("held overnight and NOT recorded", n == 0 and not fired, fired)


def boom(p):
    raise OSError("network down")


n = wl.check_dips([coin], {"LINK": 91.9}, {}, 1_000_000, False,
                  lambda c, s, p: sent.append(1), fetch=boom, log=lambda m: None)
check("candle failure is skipped, not raised", n == 0)

print("load_watchlist")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump({"coins": [{"symbol": "link"}, {"symbol": "ondo", "dip_pct": 10},
                         {"symbol": ""}]}, f)
coins = wl.load_watchlist(f.name)
check("two coins, upper-cased", [c["symbol"] for c in coins] == ["LINK", "ONDO"], coins)
check("defaults applied", coins[0]["dip_pct"] == 8.0 and coins[0]["turn_pct"] == 2.0)
check("override kept", coins[1]["dip_pct"] == 10.0)
check("product derived", coins[0]["product"] == "LINK-USD")
check("missing file -> []", wl.load_watchlist("/nonexistent.json") == [])
real = wl.load_watchlist("watchlist.json")
check("shipped watchlist has the seven coins",
      [c["symbol"] for c in real] == ["ETH", "LINK", "ONDO", "LDO", "XRP", "SOL", "BTC"],
      real)

print("early climb at the lower bar")
now = 2_000_000.0
row = {"kind": "crypto", "symbol": "LINK", "name": "Chainlink", "pct": 3.0,
       "price": 102.5, "dollars": 1.1e9}
marks = {"crypto:LINK": [[now - 60 * 60, 100.0, 1.0e9]]}
fired = {}
hits, _, _ = ab.collect_rate([row], marks, fired, now, wl.WATCH_RATE_PCT)
check("+2.5% on volume fires at the 2% watch bar", len(hits) == 1, hits)
hits3, _, _ = ab.collect_rate([row], marks, fired, now, ab.CRYPTO_RATE_PCT)
check("...but not at the general 3% bar", hits3 == [], hits3)
fired["rate:crypto:LINK"] = now - 60
hits, _, _ = ab.collect_rate([row], marks, fired, now, wl.WATCH_RATE_PCT)
check("shares the general cooldown - no double buzz", hits == [], hits)

print("run_watchlist never raises")
pushed = []
orig_push, orig_fetch = ab.push, wl.fetch_hourly
ab.push = lambda *a, **k: pushed.append(a[0])
wl.fetch_hourly = boom
try:
    ab.run_watchlist([row], marks, {}, now, False)
    check("survives a data failure", True)
finally:
    ab.push, wl.fetch_hourly = orig_push, orig_fetch

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all watchlist checks passed")
