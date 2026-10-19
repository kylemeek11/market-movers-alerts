#!/usr/bin/env python3
"""Tests for the pullback-in-uptrend alert. Run: python3 test_pullback.py"""
import pullback as pb

def series(tail):
    """A steady uptrend (so the 200-day average sits well below), then `tail`."""
    base = [100 + i * 0.2 for i in range(260)]
    return base + tail

up = series([])
last = up[-1]
# Three down days into a 10-day low, still above the 200-day average.
sig = up + [last - 1, last - 2, last - 3]
assert pb.is_signal(sig, len(sig) - 1), "3 down days to a 10-day low in an uptrend"
assert not pb.is_signal(up, len(up) - 1), "a rising day is not a signal"
# Two down days only: no signal.
assert not pb.is_signal(up + [last - 1, last - 2], len(up) + 1)
# Below the 200-day average: no signal.
crash = [100 + i * 0.2 for i in range(260)] + [50, 49, 48, 47]
assert not pb.is_signal(crash, len(crash) - 1), "downtrend is filtered out"

# Exit: the first close back above the 5-day average after a signal.
ex = sig + [last + 2]
assert pb.open_signal_exit(ex) == len(sig) - 1
assert pb.open_signal_exit(sig) is None, "signal day itself is not an exit"
# Once exited, a later up day is not a second exit.
assert pb.open_signal_exit(ex + [last + 3]) is None

sent = []
push = lambda t, m, **kw: sent.append((t, m, kw)) or True
dates = [f"d{i:04d}" for i in range(len(sig))]
st = {}
assert pb.check_pullbacks({"SPY": sig}, {"SPY": dates}, st, push) == 1
assert "SPY PULLBACK IN UPTREND" in sent[-1][0] and sent[-1][2]["loud"] is True
assert pb.check_pullbacks({"SPY": sig}, {"SPY": dates}, st, push) == 0, "once per signal"
dates2 = dates + ["d9999"]
assert pb.check_pullbacks({"SPY": ex}, {"SPY": dates2}, st, push) == 1
assert "PULLBACK EXIT" in sent[-1][0]
assert pb.check_pullbacks({"SPY": ex}, {"SPY": dates2}, st, push) == 0, "once per exit"

# The evening window: weekdays after the close only.
from datetime import datetime
assert pb.in_window(datetime(2026, 10, 19, 16, 30))       # Monday 4:30pm
assert not pb.in_window(datetime(2026, 10, 19, 15, 0))    # still trading
assert not pb.in_window(datetime(2026, 10, 18, 17, 0))    # Sunday
print("ok - pullback alert")
