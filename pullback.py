"""PULLBACK IN AN UPTREND - the one pattern that passed the Oct 2026 tests.

Rule (daily closes, SPY and QQQ):
  signal  - close above its 200-day average (long-run uptrend), down three
            days running, and at a 10-day closing low.
  entry   - the next session's open.
  exit    - the morning after the first close back above the 5-day average,
            or after 10 sessions, whichever comes first.

Backtest, 2014-2019 and 2020-2026 tested separately, 0.1% round-trip cost:
  ~10-12 signals a year, 70-75% winners, +0.56% / +0.57% average per trade
  over about 3.5 days, worst trade -3.8% / -5.0%. Roughly double what holding
  the same funds earned over the same number of days. Index ETFs generally
  pass too but fire ~50x a year; single stocks pass on average but fire ~200x
  a year with -25% to -34% worst trades, so only SPY and QQQ alert.

This module only reads completed daily bars, so it is checked once each
evening after the close. It sends two kinds of alert: the signal, and the
exit condition for a signal still open. Whether to trade either is Kyle's call.
"""

PULLBACK_SYMBOLS = ("SPY", "QQQ")
TREND_DAYS = 200
LOW_DAYS = 10
DOWN_DAYS = 3
EXIT_SMA = 5
MAX_HOLD = 10
WINDOW_START = (16, 10)        # ET: daily bar is final
WINDOW_END = (23, 59)
HISTORY = ("history 2014-2026: ~11 signals/yr, 70-75% winners, "
           "avg +0.6% over ~3.5 days, worst -5%")
KEEP_KEYS = 60


def _sma(c, i, n):
    return sum(c[i - n + 1:i + 1]) / n


def is_signal(c, i):
    """True when bar i (a completed daily close) is a pullback-in-uptrend bar."""
    if i < TREND_DAYS or i < LOW_DAYS:
        return False
    if c[i] <= _sma(c, i, TREND_DAYS):
        return False
    if not all(c[i - k] < c[i - k - 1] for k in range(DOWN_DAYS)):
        return False
    return c[i] <= min(c[i - LOW_DAYS + 1:i + 1])


def open_signal_exit(c):
    """If the latest bar is the exit bar for an open signal, return its index.

    Walks back over the last MAX_HOLD+1 bars for the most recent signal, then
    checks that no earlier bar already met the exit, and that the latest one
    does (close above the 5-day average, or the hold limit reached).
    """
    last = len(c) - 1
    for s in range(last - 1, max(last - MAX_HOLD - 1, 0) - 1, -1):
        if not is_signal(c, s):
            continue
        for j in range(s + 1, last):
            if c[j] > _sma(c, j, EXIT_SMA):
                return None          # already exited earlier
        if c[last] > _sma(c, last, EXIT_SMA) or last - s >= MAX_HOLD:
            return s
        return None
    return None


def in_window(now_et):
    if now_et is None or now_et.weekday() >= 5:
        return False
    hm = (now_et.hour, now_et.minute)
    return WINDOW_START <= hm <= WINDOW_END


def check_pullbacks(closes_by_sym, dates_by_sym, state, push, log=print):
    """Send signal / exit alerts for each symbol. Returns the number sent.

    closes_by_sym: {sym: [completed daily closes, oldest first]}
    dates_by_sym:  {sym: [matching YYYY-MM-DD strings]}
    state["pullback_sent"] dedups across runs and must survive the daily reset.
    """
    sent_keys = state.setdefault("pullback_sent", [])
    sent = 0
    for sym, c in closes_by_sym.items():
        d = dates_by_sym[sym]
        if len(c) < TREND_DAYS + 2:
            continue
        i = len(c) - 1
        if is_signal(c, i):
            key = f"sig:{sym}:{d[i]}"
            if key not in sent_keys:
                ok = push(f"{sym} PULLBACK IN UPTREND - rule triggered",
                          f"{sym} closed {c[i]:.2f}: {DOWN_DAYS} down days to a "
                          f"{LOW_DAYS}-day low, still above its 200-day average "
                          f"{_sma(c, i, TREND_DAYS):.2f}.\n"
                          f"Rule: entry at the next open; exit the morning after "
                          f"a close above the 5-day average, or after {MAX_HOLD} days.\n"
                          f"{HISTORY}",
                          priority="high", tags="chart_with_upwards_trend",
                          click=f"https://robinhood.com/stocks/{sym}", loud=True)
                if ok is not False:
                    sent_keys.append(key)
                    sent += 1
        s = open_signal_exit(c)
        if s is not None:
            key = f"exit:{sym}:{d[s]}"
            if key not in sent_keys:
                why = ("closed above its 5-day average"
                       if c[i] > _sma(c, i, EXIT_SMA) else f"{MAX_HOLD}-day limit")
                chg = (c[i] / c[s] - 1) * 100
                ok = push(f"{sym} PULLBACK EXIT - rule says sell at the open",
                          f"{sym} {why} (close {c[i]:.2f}, {chg:+.1f}% from the "
                          f"{d[s]} signal close). Rule: exit at the next open.",
                          priority="high", tags="checkered_flag",
                          click=f"https://robinhood.com/stocks/{sym}", loud=True)
                if ok is not False:
                    sent_keys.append(key)
                    sent += 1
    del sent_keys[:-KEEP_KEYS]
    if sent:
        log(f"  pullback: {sent} alert(s) sent")
    return sent


def fetch_daily(yf, sym, today_et):
    """Completed daily closes and dates. Drops a bar dated after today_et."""
    h = yf.Ticker(sym).history(period="2y", interval="1d", auto_adjust=True)
    closes, dates = [], []
    for idx, row in h.iterrows():
        ds = idx.strftime("%Y-%m-%d")
        if ds > today_et:
            continue
        closes.append(float(row["Close"]))
        dates.append(ds)
    return closes, dates
