#!/usr/bin/env python3
"""Offline tests for stop_check.py. No network, no pytest: python3 test_stop_check.py"""

import os
import sys

os.environ.setdefault("POSITIONS_FILE", "positions.json")
import stop_check as sc

FAILURES = []
HOUR = 3600
# 2026-09-24 21:00 UTC, so the "nights" below land on real calendar days.
NEWEST = 1790283600


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def make_candles(days=14, base=100.0, night_drops=None):
    """Flat hourly candles, with an optional extra drop inside each night.

    night_drops maps "days back from the newest candle" -> percent drop, applied
    to the low of the 06:00 UTC candle, which sits inside the 03:00-12:00 window.
    """
    night_drops = night_drops or {}
    candles = []
    start = NEWEST - days * 24 * HOUR
    for t in range(start, NEWEST + HOUR, HOUR):
        low = base * 0.999
        candles.append([t, low, base * 1.001, base, base, 10.0])
    by_time = {c[0]: c for c in candles}
    newest_dt = NEWEST
    for back, drop in night_drops.items():
        # 03:00 UTC of the night `back` days before the newest candle.
        day_start = newest_dt - back * 24 * HOUR
        day_start -= (day_start % (24 * HOUR))
        night_03 = day_start + 3 * HOUR
        target = by_time.get(night_03 + 3 * HOUR)
        if target:
            target[1] = base * (1 - drop / 100.0)
    return sorted(candles, key=lambda c: -c[0])   # newest first, like Coinbase


def test_stop_price():
    # The worked example: LTC at 71.90, 6% room, 0.95% spread -> 66.94
    check("LTC worked example", sc.stop_price(71.90, 6.0, 0.95, 2) == 66.94,
          sc.stop_price(71.90, 6.0, 0.95, 2))
    # NEAR at 4.7358, 7% room, 0.98% spread -> 4.3611
    check("NEAR worked example", sc.stop_price(4.7358, 7.0, 0.98, 4) == 4.3611,
          sc.stop_price(4.7358, 7.0, 0.98, 4))
    # The spread always makes the stop LOWER than the naive number, never higher.
    naive = 100 * 0.94
    check("spread widens the stop", sc.stop_price(100, 6.0, 0.95, 4) < naive)
    # Zero spread collapses to the naive number.
    check("zero spread is the naive stop",
          near(sc.stop_price(100, 10.0, 0.0, 6), 90.0))


def test_room_given_by_a_stop():
    # Inverting the formula recovers the room, which is the property the
    # notification's "x% room" claim depends on.
    price, room, spread = 50.0, 8.0, 1.0
    stop = sc.stop_price(price, room, spread, 8)
    implied = (1 - (stop / (1 - spread / 100.0)) / price) * 100.0
    check("room round-trips", near(implied, room, 1e-4), implied)


def test_overnight_drawdowns():
    candles = make_candles(days=14, night_drops={1: 5.0, 2: 1.0, 3: 2.0})
    nights = sc.overnight_drawdowns(candles)
    check("found ~13 nights", 11 <= len(nights) <= 14, len(nights))
    drops = dict((d, round(v, 3)) for d, v in nights)
    biggest = max(v for _, v in nights)
    check("catches the 5% night", near(biggest, 5.0, 0.01), biggest)
    quiet = sorted(v for _, v in nights)[0]
    check("quiet nights read ~0.1%", quiet < 0.2, quiet)
    check("nights are dated", all(len(d) == 10 for d, _ in nights))


def test_overnight_skips_incomplete_nights():
    candles = make_candles(days=14, night_drops={1: 4.0})
    full = len(sc.overnight_drawdowns(candles))
    # Punch a hole in one night: drop four of its nine hours.
    day_start = NEWEST - 1 * 24 * HOUR
    day_start -= (day_start % (24 * HOUR))
    holes = {day_start + h * HOUR for h in (3, 4, 5, 6)}
    gapped = [c for c in candles if c[0] not in holes]
    check("incomplete night is skipped",
          len(sc.overnight_drawdowns(gapped)) == full - 1)


def test_percentile():
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    check("p95 near the top", sc.percentile(vals, 0.95) in (9, 10))
    check("p50 is the middle", sc.percentile(vals, 0.5) in (5, 6))
    check("empty is None", sc.percentile([], 0.95) is None)
    check("single value", sc.percentile([7], 0.95) == 7)


def test_intraday_regime_calm():
    running, cur, typ = sc.intraday_regime(make_candles())
    check("flat market is not running", running is False, (cur, typ))


def test_intraday_regime_running():
    candles = make_candles()
    # Blow out the most recent six hours, the way LTC did on 2026-09-24.
    ordered = sorted(candles, key=lambda c: c[0])
    for c in ordered[-6:]:
        c[2] = c[3] * 1.12      # high 12% above the open
        c[1] = c[3] * 0.98
    running, cur, typ = sc.intraday_regime(ordered)
    check("spike is flagged as running", running is True, (cur, typ))
    check("current range is the big one", cur is not None and cur > 10, cur)


def test_recommend_room():
    room, why = sc.recommend_room(p95=4.0, running=False)
    check("1.5x p95", near(room, 6.0), room)
    room, _ = sc.recommend_room(p95=0.5, running=False)
    check("floor applies", room == sc.MIN_ROOM_PCT, room)
    room, _ = sc.recommend_room(p95=20.0, running=False)
    check("cap applies", room == sc.MAX_ROOM_PCT, room)
    room, why = sc.recommend_room(p95=2.0, running=True)
    check("running widens, never narrows", room == sc.RUNNING_MIN_ROOM_PCT, room)
    check("running is explained", "mid-move" in why, why)
    room, _ = sc.recommend_room(p95=None, running=False)
    check("no data falls back to the floor", room == sc.MIN_ROOM_PCT, room)
    room, why = sc.recommend_room(p95=2.0, running=True, override=7.5)
    check("override wins", room == 7.5 and why == "override")
    # A coin already wider than the running floor keeps its measured width.
    room, _ = sc.recommend_room(p95=8.0, running=True)
    check("running floor never narrows a wide coin", room == sc.MAX_ROOM_PCT, room)


def test_evaluate():
    pos = {"symbol": "TEST", "product": "TEST-USD", "qty": 100,
           "avg_cost": 90.0, "spread_pct": 1.0, "current_stop": None,
           "room_override_pct": None, "decimals": 2}
    ev = sc.evaluate(pos, 100.0, make_candles(night_drops={1: 4.0, 2: 3.0}))
    check("risk is qty x (price - stop)",
          near(ev["risk"], 100 * (100.0 - ev["stop"]), 1e-6), ev["risk"])
    check("value is qty x price", near(ev["value"], 10000.0))
    check("vs_cost measured against average cost",
          near(ev["vs_cost"], (ev["stop"] / 90.0 - 1) * 100, 1e-9))
    check("stop is below the price", ev["stop"] < 100.0)
    check("nights counted", ev["nights"] >= 10, ev["nights"])


def test_alert_reason():
    base = {"price": 100.0, "stop": 94.0, "running": False}

    def ev(**kw):
        d = dict(base)
        d.update(kw)
        return d

    check("no stop set alerts",
          sc.alert_reason(ev(current_stop=None), False) == "no stop set")
    check("stop above market alerts",
          "will fire" in sc.alert_reason(ev(current_stop=99.9), False))
    r = sc.alert_reason(ev(current_stop=88.0), False)
    check("6% drift alerts in the day", r and "too loose" in r, r)
    check("6% drift also alerts overnight",
          sc.alert_reason(ev(current_stop=88.0), True) is not None)
    check("1% drift is quiet",
          sc.alert_reason(ev(current_stop=95.0), False) is None)
    # 3% drift: worth a daytime nudge, not worth waking him up.
    check("3% drift alerts in the day",
          sc.alert_reason(ev(current_stop=91.0), False) is not None)
    check("3% drift stays quiet overnight",
          sc.alert_reason(ev(current_stop=91.0), True) is None)
    r = sc.alert_reason(ev(current_stop=97.0), False)
    check("a too-tight stop is named as such", r and "too tight" in r, r)


def test_suppression():
    ev = {"symbol": "LTC", "stop": 66.94, "price": 71.90}
    now = 1_000_000.0
    fresh = {"LTC": {"stop": 66.90, "ts": now - 3600}}
    check("same advice within the window is suppressed",
          sc.suppressed(ev, fresh, now) is True)
    old = {"LTC": {"stop": 66.90, "ts": now - 9 * 3600}}
    check("stale advice is not suppressed",
          sc.suppressed(ev, old, now) is False)
    moved = {"LTC": {"stop": 64.00, "ts": now - 3600}}
    check("a materially moved stop is not suppressed",
          sc.suppressed(ev, moved, now) is False)
    check("unknown symbol is not suppressed",
          sc.suppressed(ev, {}, now) is False)


def test_config_is_loadable():
    import json
    from pathlib import Path
    cfg = json.loads(Path("positions.json").read_text())
    positions = cfg["positions"]
    check("four positions", len(positions) == 4, len(positions))
    required = {"symbol", "product", "qty", "avg_cost", "spread_pct",
                "current_stop", "room_override_pct", "decimals"}
    for p in positions:
        check(f"{p['symbol']} has every field",
              required <= set(p), sorted(required - set(p)))
        check(f"{p['symbol']} spread is a plausible percent",
              0 < float(p["spread_pct"]) < 5, p["spread_pct"])
        check(f"{p['symbol']} quantity is positive", float(p["qty"]) > 0)


def test_no_trading_surface():
    """The script must have no way to place an order. Belt and braces."""
    from pathlib import Path
    src = Path("stop_check.py").read_text().lower()
    for word in ("robinhood.com/api", "nummus", "place_order", "sell(",
                 "buy(", "password", "bearer "):
        check(f"no trading surface: {word!r} absent", word not in src)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n{t.__name__}")
        t()
    print("\n" + "=" * 50)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("all tests passed")


if __name__ == "__main__":
    main()
