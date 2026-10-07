#!/usr/bin/env python3
"""Tests for daily_context.py and its hooks in alerts_bot.py.

Run: python3 test_daily_context.py   (no pytest, no network)
"""

import json
import sys
from datetime import datetime, timedelta, timezone

import daily_context as dc
import alerts_bot as ab

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# A Tuesday, 02:00 UTC = Monday 9pm Central.
NOW = datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc).timestamp()
# Saturday 15:00 UTC.
SAT = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc).timestamp()


def day_ts(days_ago, now=NOW):
    d = datetime.fromtimestamp(now, tz=timezone.utc).replace(hour=0, minute=0,
                                                              second=0, microsecond=0)
    return int((d - timedelta(days=days_ago)).timestamp())


def candles(closes, now=NOW, today_close=None, today_high=None):
    """Daily candles [time, low, high, open, close, volume], newest first.

    closes[0] is yesterday (the newest COMPLETED day). An in-progress candle
    for today is added when today_close is given.
    """
    rows = []
    if today_close is not None:
        hi = today_high if today_high is not None else today_close
        rows.append([day_ts(0, now), today_close * 0.99, hi, closes[0], today_close, 1000])
    for i, c in enumerate(closes):
        rows.append([day_ts(i + 1, now), c * 0.98, c * 1.02, c, c, 1000])
    return rows


def flat_history(level=1.0, n=60, wiggle=0.02):
    """n completed closes bobbing +-wiggle around level: a tight base."""
    out = []
    for i in range(n):
        out.append(level * (1 + wiggle * (1 if i % 2 else -1)))
    return out


print("summarize")
closes = flat_history(1.0)
s = dc.summarize(candles(closes, today_close=1.07, today_high=1.09), NOW)
check("prev_close is yesterday's close", abs(s["prev_close"] - closes[0]) < 1e-9)
check("base14 measured on completed closes", abs(s["base14"] - 4.0816) < 0.01,
      s["base14"])
check("avg50 is the mean of 50 completed closes",
      abs(s["avg50"] - sum(closes[:50]) / 50) < 1e-9)
check("today's intraday high joins the 30-day high", abs(s["high30"] - 1.09) < 1e-9)
# Alternating +-2% closes: up moves are +4.08%, down moves -3.92%, mean 4.00%.
check("vol30 is the mean absolute daily move", abs(s["vol30"] - 4.0016) < 0.01, s["vol30"])
check("recent daily returns kept, newest first", len(s["days"]) == 5)
check("too little history -> None", dc.summarize(candles(closes[:10]), NOW) is None)
s2 = dc.summarize(candles(closes[:40]), NOW)
check("40 days: base ok, no 50-day opinion", s2 is not None and s2["avg50"] is not None)
s3 = dc.summarize(candles(closes[:20]), NOW)
check("20 days: base ok, trend unknown", s3 is not None and s3["avg50"] is None)
check("garbage candles are skipped", dc.summarize([["x"], None, [1, 2]], NOW) is None)

print("coin_context / is_breakout")
entry = dc.summarize(candles(closes, today_close=1.06), NOW)
ctx = dc.coin_context(entry, 1.06, dc.utc_day(NOW))
check("day_pct against yesterday's close", abs(ctx["day_pct"] - (1.06 / closes[0] - 1) * 100) < 1e-9)
green, why = dc.is_breakout(ctx, NOW)
check("tight base + 5% day + above 50-day + weekday = green", green, why)
green, why = dc.is_breakout(dc.coin_context(entry, closes[0] * 1.04, dc.utc_day(NOW)), NOW)
check("+4% is not a breakout", not green and why == "no +5% day", why)
green, why = dc.is_breakout(ctx, SAT)
check("same setup on a Saturday is not green", not green and why == "weekend", why)
loose = dc.summarize(candles([1.0, 1.3, 0.9, 1.2, 1.0] + flat_history(1.0, 55)), NOW)
green, why = dc.is_breakout(dc.coin_context(loose, 1.06, dc.utc_day(NOW)), NOW)
check("loose base is not a breakout", not green and why == "base not tight", why)
# Tight recent base but far below a 50-day average (it fell, then went quiet).
fallen = dc.summarize(candles(flat_history(1.0, 14) + flat_history(2.0, 46)), NOW)
green, why = dc.is_breakout(dc.coin_context(fallen, 1.06, dc.utc_day(NOW)), NOW)
check("below the 50-day average is not green", not green and why == "below 50-day average", why)
check("stale cache entry is ignored", dc.coin_context(entry, 1.06, "2026-10-07") is None)
check("no entry -> not green", dc.is_breakout(None, NOW) == (False, "no daily history"))

print("btc_lines")
check("quiet BTC -> no lines", dc.btc_lines({"day_pct": -1.0, "ret3_pct": 0.9, "days": [-0.9, 2.1, 0.3]}) == [])
lines = dc.btc_lines({"day_pct": -3.4, "ret3_pct": -2.0, "days": [-1.0, -1.0, -1.0]})
check("BTC -3% in 24h is a red line", len(lines) == 1 and lines[0].startswith("RED: BTC -3.4% in 24h"), lines)
lines = dc.btc_lines({"day_pct": -1.0, "ret3_pct": -5.5, "days": [-2.0, -2.0, -2.0]},
                     {"ret3_pct": 1.5})
check("BTC -5% over 3 days, coin held up -> catches-down warning",
      len(lines) == 1 and "held up" in lines[0], lines)
lines = dc.btc_lines({"day_pct": -1.0, "ret3_pct": -5.5, "days": [-2.0, -2.0, -2.0]},
                     {"ret3_pct": -4.0})
check("...and no held-up clause when the coin fell too", "held up" not in lines[0], lines)
lines = dc.btc_lines({"day_pct": 0.5, "ret3_pct": -4.0, "days": [0.5, -6.2, 1.0]})
check("a BTC -5% day two days ago -> wait line",
      len(lines) == 1 and "2 days ago" in lines[0] and "wait 3 days" in lines[0], lines)
lines = dc.btc_lines({"day_pct": 0.5, "ret3_pct": 1.0, "days": [0.5, 1.0, 1.0, -7.0]})
check("a BTC -5% day four days ago is forgotten", lines == [], lines)
lines = dc.btc_lines({"day_pct": 1.0, "ret3_pct": 6.0, "days": [2.0, 2.0, 2.0]},
                     {"ret3_pct": 0.3})
check("BTC +5% over 3 days and the coin flat -> no catch-up line",
      len(lines) == 1 and lines[0].startswith("no catch-up trade"), lines)
lines = dc.btc_lines({"day_pct": 1.0, "ret3_pct": 6.0, "days": [2.0, 2.0, 2.0]},
                     {"ret3_pct": 4.0})
check("...but a coin already following BTC gets nothing", lines == [], lines)
check("no BTC data -> no lines", dc.btc_lines(None) == [])

print("coin_lines")
check("calm coin near its high -> nothing", dc.coin_lines({"vol30": 2.7, "off_high_pct": -4.0}) == [])
lines = dc.coin_lines({"vol30": 6.4, "off_high_pct": -4.0})
check("over 5%/day -> half-size flag", len(lines) == 1 and "half size" in lines[0], lines)
lines = dc.coin_lines({"vol30": 3.0, "off_high_pct": -28.7})
check("deep dip -> red line", len(lines) == 1 and lines[0].startswith("RED: 29% below"), lines)
check("no context -> nothing", dc.coin_lines(None) == [])

print("Bitstamp depth")
book = {"bids": [["2.30", "1000"], ["2.20", "5000"], ["1.97", "40000"], ["1.50", "100000"]],
        "asks": [["2.31", "1000"]]}
depth = dc.book_depth_usd(book)
# mid 2.305, floor 1.95925: the 2.30, 2.20 and 1.97 levels count, 1.50 does not.
check("bids within 15% of mid are summed", abs(depth - (2300 + 11000 + 78800)) < 1e-6, depth)
check("empty book -> None", dc.book_depth_usd({"bids": [], "asks": []}) is None)

PAIRS = [{"name": "BTC/USD", "url_symbol": "btcusd", "trading": "Enabled"},
         {"name": "ORCA/USD", "url_symbol": "orcausd", "trading": "Enabled"},
         {"name": "BTC/EUR", "url_symbol": "btceur", "trading": "Enabled"},
         {"name": "OLD/USD", "url_symbol": "oldusd", "trading": "Disabled"}]


def fake_fetch(url):
    if url.endswith("/trading-pairs-info/"):
        return PAIRS
    if url.endswith("/order_book/orcausd/"):
        return book
    if url.endswith("/order_book/btcusd/"):
        return {"bids": [["85000", "100"]], "asks": [["85001", "1"]]}
    raise RuntimeError("unexpected " + url)


st = {}
pairs = dc.bitstamp_pairs(st, NOW, fetch=fake_fetch, log=lambda m: None)
check("USD pairs only, disabled pairs dropped", pairs == {"BTC": "btcusd", "ORCA": "orcausd"}, pairs)
check("pair list cached for the day", st["daily"]["_bitstamp"]["d"] == dc.utc_day(NOW))
line = dc.thin_book_line(st, "ORCA", NOW, fetch=fake_fetch, log=lambda m: None)
check("thin ORCA book -> market-maker line", line is not None and line.startswith("thin book: $92K"), line)
line = dc.thin_book_line(st, "BTC", NOW, fetch=fake_fetch, log=lambda m: None)
check("deep BTC book -> exchange routing OK", line is not None and "exchange routing OK" in line, line)
line = dc.thin_book_line(st, "MEW", NOW, fetch=fake_fetch, log=lambda m: None)
check("coin Bitstamp does not list -> market-maker only", line == "not on Bitstamp - market-maker routing only for the stop", line)


def failing_fetch(url):
    raise RuntimeError("down")


check("Bitstamp down, nothing cached -> no line",
      dc.thin_book_line({}, "ORCA", NOW, fetch=failing_fetch, log=lambda m: None) is None)
check("Bitstamp down, pairs cached -> still no guess",
      dc.thin_book_line(st, "ORCA", NOW, fetch=failing_fetch, log=lambda m: None) is None)

print("refresh")
UNIVERSE = [{"kind": "crypto", "symbol": s, "price": 1.0}
            for s in ["SOL", "HYPE", "MEW", "NEWCOIN", "FLAKY", "BTC"]]
calls = []


def fetch_fake(sym, now_ts):
    calls.append(sym)
    if sym == "MEW":
        return "missing"
    if sym == "FLAKY":
        return None
    if sym == "NEWCOIN":
        return candles(flat_history(1.0, 5), today_close=1.0)
    return candles(flat_history(1.0), today_close=1.0)


st = {}
n = dc.refresh(st, UNIVERSE, NOW, fetch=fetch_fake, budget=3, log=lambda m: None,
               sleep=lambda s: None)
check("budget limits requests per run", n == 3 and calls == ["BTC", "FLAKY", "HYPE"], calls)
calls.clear()
n = dc.refresh(st, UNIVERSE, NOW, fetch=fetch_fake, budget=10, log=lambda m: None,
               sleep=lambda s: None)
check("next run finishes the rest, retrying the failed one",
      sorted(calls) == ["FLAKY", "MEW", "NEWCOIN", "SOL"], calls)
check("404 lands in cb_missing", st["cb_missing"].get("MEW") is True)
check("short listing remembered for the day", st["daily"]["NEWCOIN"].get("short") is True)
check("good coins summarized", st["daily"]["SOL"]["d"] == dc.utc_day(NOW) and "avg50" in st["daily"]["SOL"])
calls.clear()
n = dc.refresh(st, UNIVERSE, NOW, fetch=fetch_fake, budget=10, log=lambda m: None,
               sleep=lambda s: None)
check("still-failing coin is the only refetch", calls == ["FLAKY"] and n == 1, calls)
tomorrow = NOW + 86400
calls.clear()
dc.refresh(st, UNIVERSE[:2], tomorrow, fetch=fetch_fake, budget=10, log=lambda m: None,
           sleep=lambda s: None)
check("a new day refetches everything in the universe", sorted(calls) == ["BTC", "HYPE", "SOL"], calls)
check("coins that left the universe are forgotten", "NEWCOIN" not in st["daily"])

print("collect_breakouts")
st = {"daily": {"HYPE": dc.summarize(candles(flat_history(90.0), today_close=95.0), NOW),
                "SOL": dc.summarize(candles(flat_history(200.0), today_close=203.0), NOW)}}
rows = [{"kind": "crypto", "symbol": "HYPE", "name": "Hyperliquid", "price": 95.0, "pct": 5.6,
         "dollars": 3.6e7, "cap": 3e10, "week_pct": 9.3},
        {"kind": "crypto", "symbol": "SOL", "name": "Solana", "price": 203.0, "pct": 1.5,
         "dollars": 1e8, "cap": 1e11, "week_pct": 1.1}]
fired = {}
hits = dc.collect_breakouts(st, rows, fired, NOW)
check("first sighting is remembered, not sent", hits == [] and "HYPE" in st["base_seen"]
      and "SOL" not in st["base_seen"], st.get("base_seen"))
hits = dc.collect_breakouts(st, rows, fired, NOW + 5 * 60)
check("five minutes later: still waiting", hits == [])
hits = dc.collect_breakouts(st, rows, fired, NOW + 10 * 60)
check("ten minutes later, still +5.6%: HYPE breaks out, SOL at +1.5% does not",
      [h[1]["symbol"] for h in hits] == ["HYPE"])
fired[hits[0][0]] = dc.utc_day(NOW)
check("one alert per coin per UTC day", dc.collect_breakouts(st, rows, fired, NOW + 20 * 60) == [])
# A wick: green once, gone the next run, green again -> the clock restarts.
st2 = {"daily": dict(st["daily"])}
dc.collect_breakouts(st2, rows, {}, NOW)
faded = [dict(rows[0], price=91.0), rows[1]]      # +3.2% on the day
dc.collect_breakouts(st2, faded, {}, NOW + 5 * 60)
check("dropping back under the bar forgets the sighting", "HYPE" not in st2["base_seen"])
hits = dc.collect_breakouts(st2, rows, {}, NOW + 10 * 60)
check("...so a return to +5% starts the clock again", hits == [] and "HYPE" in st2["base_seen"])
check("tomorrow, yesterday's cache entry is stale and fires nothing",
      dc.collect_breakouts(st, rows, fired, NOW + 86400) == [])
st_tomorrow = {"daily": {"HYPE": dict(st["daily"]["HYPE"], d=dc.utc_day(NOW + 86400))},
               "base_seen": {"HYPE": NOW + 86400 - 900}}
check("...once the cache is refreshed", len(dc.collect_breakouts(st_tomorrow, rows, {}, NOW + 86400)) == 1)

print("night_pass")
night = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc).timestamp()   # 3am CDT
st = {}
check("overnight: first two pass", dc.night_pass(st, night) and dc.night_pass(st, night))
check("...third is held", not dc.night_pass(st, night))
check("daytime always passes",
      dc.night_pass(st, datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc).timestamp()))
check("the count belongs to the night that began yesterday evening",
      st["night_pass"]["night"] == "2026-10-05", st["night_pass"])
late_evening = datetime(2026, 10, 7, 3, 30, tzinfo=timezone.utc).timestamp()  # 10:30pm CDT Oct 6
check("a new night resets the count", dc.night_pass(st, late_evening)
      and st["night_pass"]["night"] == "2026-10-06" and st["night_pass"]["count"] == 1)

# --- Hooks in alerts_bot ----------------------------------------------------
print("alerts_bot hooks")


def no_network(url, timeout=None):
    raise RuntimeError("network disabled in tests: " + url)


dc._get_json = no_network
SENT = []
ab.push = lambda title, message, **kw: SENT.append((title, message, kw))
ab.robinhood_tradable = lambda row, cache: True

ONE_AM = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc).timestamp()   # 1am CDT
hype = {"kind": "crypto", "symbol": "HYPE", "name": "Hyperliquid", "price": 95.0, "pct": 5.6,
        "dollars": 3.6e7, "cap": 3e10, "week_pct": 9.3, "low_24h": 90.0, "rv_source": "coinbase"}
plain = dict(hype, symbol="SOL", name="Solana", pct=12.0)

# A rate alert at +3.4% overnight: held unless it is a TOP SETUP.
state = {"alerts_today": {}, "streak": {}, "daily": {}}
fired = {}
SENT.clear()
ab.send_rate([("rate:crypto:SOL", plain, 3.4, 60, 2.8)], fired, ONE_AM, True, state=state)
check("ordinary overnight rate alert is still held", SENT == [] and fired == {})
ab.send_rate([("rate:crypto:HYPE", hype, 3.4, 60, 6.0)], fired, ONE_AM, True, state=state)
check("TOP SETUP passes the overnight bar", len(SENT) == 1 and SENT[0][0].startswith("* HYPE"),
      SENT[:1])
check("...at max priority", SENT[0][2].get("priority") == "max")
check("...and counts against the night cap", state["night_pass"]["count"] == 1)
SENT.clear()
state["night_pass"]["count"] = dc.NIGHT_PASS_MAX
fired = {}
ab.send_rate([("rate:crypto:HYPE", hype, 3.4, 60, 6.0)], fired, ONE_AM, True, state=state)
check("cap reached: even a TOP SETUP waits for morning", SENT == [] and fired == {})

# Breakout alert body.
state = {"alerts_today": {}, "streak": {},
         "daily": {"HYPE": dc.summarize(candles(flat_history(90.0), today_close=95.0), NOW),
                   "BTC": dc.summarize(candles(flat_history(85000.0), today_close=85500.0), NOW),
                   "_bitstamp": {"d": dc.utc_day(NOW), "pairs": {"BTC": "btcusd"}}}}
SENT.clear()
fired = {}
state["base_seen"] = {"HYPE": NOW - 15 * 60}        # seen a quarter-hour ago
ab.run_breakouts([hype], fired, NOW, False, state, {}, 85500.0)
check("breakout alert sent", len(SENT) == 1, SENT)
title, body, kw = SENT[0]
check("starred title with the day move", title.startswith("* HYPE base breakout +"), title)
check("body names the base and the trend", "14-day base" in body and "vs 50-day avg" in body, body)
check("body carries the study line", "run 30% / stop 11%" in body)
check("not on Bitstamp -> market-maker routing line", "market-maker routing only" in body, body)
check("stop hint present", "15% stop" in body)
check("1st alert today tag", "1st alert today" in body)
check("fired once for the day", fired.get("base:crypto:HYPE") == dc.utc_day(NOW))
check("high priority in daytime", kw.get("priority") == "high" and kw.get("tags") == "star")
SENT.clear()
ab.run_breakouts([hype], fired, NOW, False, state, {}, 85500.0)
check("no repeat the same day", SENT == [])

# Red lines strip the star.
hot = dict(hype, week_pct=27.0)
SENT.clear()
state["base_seen"] = {"HYPE": NOW - 15 * 60}
ab.run_breakouts([hot], {}, NOW, False, state, {}, 85500.0)
check("hot week -> caution line, no star", SENT and not SENT[0][0].startswith("*")
      and "caution: already up 27%" in SENT[0][1] and SENT[0][2].get("tags") == "zap", SENT[:1])
state["daily"]["BTC"]["prev_close"] = 90000.0    # BTC -5% today
SENT.clear()
state["base_seen"] = {"HYPE": NOW - 15 * 60}
ab.run_breakouts([hype], {}, NOW, False, state, {}, 85500.0)
check("BTC dump day -> RED line on the breakout", SENT and "RED: BTC -5.0% in 24h" in SENT[0][1], SENT[:1])
state["daily"]["BTC"]["prev_close"] = 85000.0

# Context lines ride on threshold alerts too.
SENT.clear()
state["daily"]["HYPE"]["vol30"] = 6.4
ab.send_alerts([(15.0, "crypto:HYPE", dict(hype, pct=16.0))], {}, ab.CRYPTO_HIGH_PRIORITY_LEVEL,
               False, state=state, btc_price=85500.0, now_ts=NOW)
check("threshold alert carries the volatility flag", SENT and "half size" in SENT[0][1], SENT[:1])
# Regression (2026-10-07): send_alerts used to re-read the clock instead of
# taking the run's. The daily cache is keyed by UTC day, so on any day but
# the one NOW is pinned to, coin_context saw a stale entry and every
# checklist line vanished from threshold alerts - and this suite only
# passed on 2026-10-06.
SENT.clear()
ab.send_alerts([(15.0, "crypto:HYPE", dict(hype, pct=16.0))], {}, ab.CRYPTO_HIGH_PRIORITY_LEVEL,
               False, state=state, btc_price=85500.0, now_ts=NOW + 86400)
check("a threshold alert a day later drops the stale checklist lines",
      SENT and "half size" not in SENT[0][1], SENT[:1])

# Overnight breakout: passes, then the cap holds it.
state["night_pass"] = {}
SENT.clear()
state["base_seen"] = {"HYPE": ONE_AM - 15 * 60}
ab.run_breakouts([hype], {}, ONE_AM, True, state, {}, 85500.0)
check("overnight breakout goes out at max", SENT and SENT[0][2].get("priority") == "max", SENT[:1])
state["night_pass"]["count"] = dc.NIGHT_PASS_MAX
SENT.clear()
state["base_seen"] = {"HYPE": ONE_AM - 15 * 60}
ab.run_breakouts([hype], {}, ONE_AM, True, state, {}, 85500.0)
check("overnight cap holds the breakout", SENT == [])

# Without the module nothing breaks and nothing passes.
saved = ab.dc
ab.dc = None
SENT.clear()
state = {"alerts_today": {}, "streak": {}}
ab.send_rate([("rate:crypto:HYPE", hype, 3.4, 60, 6.0)], {}, ONE_AM, True, state=state)
check("no module: TOP SETUP held overnight as before", SENT == [])
ab.run_breakouts([hype], {}, NOW, False, state, {}, 85500.0)
ab.refresh_daily_context(state, [hype], NOW)
ab.send_alerts([(15.0, "crypto:HYPE", dict(hype, pct=16.0))], {}, ab.CRYPTO_HIGH_PRIORITY_LEVEL,
               False, state=state, btc_price=85500.0)
check("no module: threshold alert still sent, no extra lines",
      len(SENT) == 1 and "half size" not in SENT[0][1])
ab.dc = saved

# A stock row never gets crypto context.
check("stocks get no context lines",
      ab.context_lines({"daily": {}}, {"kind": "stock", "symbol": "ABC", "price": 5.0}, 85500.0, NOW) == [])

print()
if FAILURES:
    print(f"{len(FAILURES)} failed: {FAILURES}")
    sys.exit(1)
print("all daily-context checks passed")
