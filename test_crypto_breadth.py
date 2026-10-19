#!/usr/bin/env python3
"""Tests for the crypto-breadth alert. Run: python3 test_crypto_breadth.py"""
import time
import alerts_bot as ab

sent = []
ab.push = lambda title, msg, **kw: sent.append((title, msg, kw)) or True

def rows(offs):
    return [{"symbol": f"C{i}", "price": 100 * (1 + o / 100), "low_24h": 100.0,
             "cap": 1e9 - i} for i, o in enumerate(offs)]

now = time.time()
# Oct 18 shape: most coins 4-7% off their lows.
st = {}
r = rows([7.0, 5.9, 5.8, 5.6, 5.5, 4.8, 4.5, 4.5, 4.4, 4.4, 4.0, 4.0, 3.6, 3.5, 1.7])
c, n, med, lead = ab.crypto_breadth(r)
assert (c, n) == (12, 15) and round(med, 1) == 4.5, (c, n, med)
assert ab.send_crypto_breadth(r, st, now) == 1
assert "12 of 15" in sent[-1][0] and sent[-1][2]["priority"] == "high"
assert ab.send_crypto_breadth(r, st, now + 300) == 0, "once per level per day"
# A bigger run crosses the urgent level.
r2 = rows([9.0] * 12 + [3.0] * 3)
assert ab.send_crypto_breadth(r2, st, now + 600) == 1 and sent[-1][2]["priority"] == "max"
# Overnight: only the urgent level goes out.
st2 = {}
assert ab.send_crypto_breadth(r, st2, now, overnight=True) == 0
# Narrow move: one coin running is not breadth.
st3 = {}
assert ab.send_crypto_breadth(rows([20.0] + [1.0] * 14), st3, now) == 0
# Too few coins with data: stay quiet.
assert ab.send_crypto_breadth(rows([8.0] * 5), {}, now) == 0
print("ok - crypto breadth alert")
