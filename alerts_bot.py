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
is one CoinGecko call plus a Coinbase candle request for each coin that has
already cleared the price bar, so a run finishes in seconds.

Two feeds (2026-10-16, Kyle's "quieter alerts"). Only an alert the standing
rules would let him buy makes a sound, and only it carries the gold star: a
BASE BREAKOUT with no RED / LATE / no-catch-up line, between 7 AM and 7 PM
Central, on a coin not already held, with the book under its limits
(positions.json: lot count, crypto cost cap, the crash budget, the
"_buys_allowed" switch). Its title starts with the coin, BUY and the size.
Everything else - rate alerts, threshold crossings, stocks, relative volume,
dips - goes to the quiet feed (NTFY_TOPIC + "-quiet", low priority), which is
there to scroll, not to buzz. Setups that arrive outside the window are
re-checked once by the first run after 7 AM. The peer-reviewed reason:
pushes about movers raise retail risk-taking (Arnold, Pelster & Subrahmanyam,
JFE 2022) and Robinhood users' top buys averaged -4.7% over the next 20 days
(Barber, Huang, Odean & Schwarz, JF 2022).

Mover pushes off (2026-10-19, Kyle's "fix it"): the rate, threshold, dip,
volume and market-wide-run pushes are no longer sent at all while
MOVER_PUSHES is False - they are logged as "[off] ..." and their bookkeeping
still runs. See MOVER_PUSHES.

Stop watch (same day): a held coin trading under the sell limit of its stop
(5% under the stop price) gets a loud "check your stops", because a triggered
crypto stop cannot sell below that and Robinhood may cancel it after five
minutes. A failed push is tried twice and the run exits non-zero.

Venue and peg watch (2026-10-25, rulebook section 9 "News and events"): the
two kinds of news with a measured, immediate effect that a stop cannot see
coming. Robinhood's status page reporting trouble with trading while lots are
held is a loud push (a stop is only protection while the venue can trade);
Coinbase trouble is a quiet note (the bot's prices come from there). USDC or
USDT 0.5% or more off the dollar on Kraken is a loud push: a USDT depeg
raises bitcoin's five-minute jump odds about fivefold (Perez Riaza & Gnabo
2025). Both are information for Kyle, not rules: the entry, exit and sizing
rules are frozen until the Nov 5 review.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pullback

# The daily checklist (tight-base breakouts, BTC red lights, volatility flag,
# Bitstamp depth, overnight pass for green signals) lives in its own module.
# Optional on purpose: if the file is missing, every alert still goes out
# exactly as before, just without the extra lines and the breakout alert.
try:
    import daily_context as dc
except Exception:                      # ImportError, or a broken upload
    dc = None

# --- Stock settings (match market_movers.py) --------------------------------
MIN_GAIN_PCT = 5.0
MIN_DOLLAR_VOLUME = 25_000_000
MIN_MARKET_CAP = 100_000_000
MIN_PRICE = 3.00
# Kyle only wants stocks he can take a real position in, so the screen is
# capped rather than just floored. This goes into the Yahoo query itself, not
# just the local check below: the screener returns at most 250 names sorted by
# percent change, so filtering server-side means those 250 are the best movers
# UNDER $10 rather than the best movers overall, of which only about a quarter
# were ever under $10.
MAX_PRICE = 10.00
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
CRYPTO_MIN_24H_VOLUME = 2_000_000      # USD traded in 24h
CRYPTO_MIN_MARKET_CAP = 20_000_000
CRYPTO_TOP_N = 250                     # how many coins by market cap to watch
# Every Robinhood coin is watched, not just the top 250 (2026-09-28).
# Measured over the month to 09-28: 20 of Robinhood's 87 coins were invisible
# to the bot - outside the top 250 (MEGA, MOODENG, ZORA, BILL...) or under the
# old $10M volume / $50M cap gates (SKR, FLR, XTZ...). They ran MORE, not
# less: 2.7 runs of 20%+ per coin against 2.3 for the coins the bot could see,
# and they included SKR +246% and ZORA +102%. Across all 396 Coinbase coins,
# runs were most frequent in the smallest, cheapest coins - price per coin is
# not the cause (it only reflects how many coins exist), size and liquidity
# are. So the gates now only keep out coins too thin to fill a ~$1-2K order
# cleanly, and the Robinhood coins outside the top 250 are fetched by symbol.
CRYPTO_FETCH_MISSING_RH = True
# Coins below this market cap get a warning line: they run more often but
# fall just as hard, so the stop goes in the moment the buy fills.
SMALL_CAP_USD = 100_000_000

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
# COOLDOWN (raised 45min -> 3h on 2026-10-07). The 09-27 replay bucket above
# is defined over 3 hours - first alert for a coin in 3h averaged +3.0% (47%
# positive), a repeat inside that window -0.1% (38%), a third or later -0.9%
# - so the cooldown now matches the window the edge was measured in. The
# live 10-07 session reproduced it on one coin: RAY's 06:28 alert had +10.6%
# still to come, and its 07:13 / 08:03 / 08:48 repeats had +6.9 / +3.4 / +1.0
# and finished negative. Position management while already holding comes from
# the stop-check workflow, not from re-buzzing the entry signal.
RATE_COOLDOWN_SEC = 3 * 3600    # re-alert the same name at most this often
RATE_MARKS_KEPT = 24            # enough stamps to span the window with drift

# WHERE THE VOLUME FIGURE COMES FROM (2026-09-27). CoinGecko's counter is a
# GLOBAL 24h total across every exchange, and it is slow to move. Quant on
# 2026-09-24: Coinbase's own tape ran 3-10x normal from 07:55 CT while the bot
# logged "coins climbing on ordinary volume" at 07:51, 08:00 and 09:20 and
# never flagged it; the first QNT alert was the 15% day threshold at 11:23 CT,
# $84. The same rule replayed on Coinbase volume fires at 07:55 CT at $75.14.
# So for crypto, once a coin has cleared the PRICE bar, the volume check now
# comes from Coinbase's 5-minute candles: dollars traded in the window against
# that coin's own pace for a window that long over the previous 24 hours. Same
# 2.5x scale as before (the old formula reduces to the same ratio when the
# counter is fresh). CoinGecko remains the fallback for coins Coinbase does not
# list and for the shadow counts in rate_bars. Only the handful of coins over
# the price bar cost a request, so a run stays quick.
COINBASE_API = "https://api.exchange.coinbase.com"
COINBASE_VOLUME_CONFIRM = True
COINBASE_TIMEOUT = 10
COINBASE_BASELINE_H = 24        # "normal pace" is measured over this long
COINBASE_MIN_BASELINE_MIN = 6 * 60   # less history than this -> no opinion

# ALERT TAGS (2026-09-27). Replaying the live rule over 79 Robinhood-tradable
# coins for 4.9 days (307 alerts with a day of outcome), under a 10% trailing
# stop after costs: the FIRST alert for a coin in 3h averaged +3.0% (47%
# positive); a repeat within 3h averaged -0.1% (38%); a third or later -0.9%.
# Repeats touch +5% more often - they are just bought nearer the top. So each
# rate alert now says whether it is the first buzz today or repeat #N, and a
# small memory that survives the daily state reset says when the same coin
# has been alerting for several days running (Quant alerted on 24, 25, 26 and
# 27 Sep - that persistence was the tell, and no single alert showed it).
STREAK_KEEP_DAYS = 2            # forget a name this many days after its last alert

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

# Overnight rate bar. THIS WAS A BUG until 2026-09-23: send_rate held any
# overnight rate signal below `high_bar`, and `high_bar` was the DAY threshold
# - 40% for crypto. A rate move is measured over 50-75 minutes and is
# typically 3-6%, so it could never clear 40 and every single overnight rate
# alert was suppressed. Raydium ran 03:51-08:21 CDT on 2026-09-23 for +11.5%;
# the signal found it at 05:00 and held it until the move was over.
#
# Measured on 16 Robinhood-tradable coins over 5 nights (22:00-07:00 CDT,
# 45-minute cooldown, price only - the live 2.5x volume test cuts these
# further):
#     3% -> 6.8 wake-ups/night      5% -> 1.8/night
#     4% -> 3.0 wake-ups/night      6% -> 1.5/night
# Replayed against Raydium: 4% fires 05:00 CDT at $1.8585 with 6.7% still to
# come. 5% never fires at all. 4.0 is the bar that would have caught it.
OVERNIGHT_RATE_PCT = 4.0

# Execution cost on Robinhood crypto, measured from Kyle's own fills on
# 2026-09-23: ten orders across ETH, DOGE, LTC, UNI, NEAR and RAY, buys and
# sells. Every one landed OUTSIDE the real market's one-minute range, mean
# 0.944% per side, range 0.81-1.06%, with no separate fee line - the cost is
# inside the fill price. It is the same ~0.94% on ETH (real spread 0.0007%) as
# on RAY, so it tracks no actual liquidity cost and does not shrink on the
# majors.
#
# Two consequences the alerts now carry:
#   - A move has to clear ~1.9% round trip before it is worth anything. The
#     alert fires at 4%, so roughly half of the move it is reporting is gone.
#   - The stop is evaluated against Robinhood's marked-down bid, so it trips
#     ~0.94% EARLY in real-market terms. NEAR on 2026-09-17 was stopped at
#     $3.3942 against a $3.40 stop when the market's lowest print was $3.4144
#     - the market never reached the stop. Set the stop wider to compensate.
EXEC_MARKUP_PCT = 0.94
# STOP HINT WIDENED 10 -> 15 on 2026-09-27. Sixty days of hourly Coinbase data,
# 81 Robinhood-tradable coins, every fire of the live rule (578 alerts, each
# followed for a week, costs included), trailing stop from the alert price:
#     width   all alerts        first alert of the day
#     10%     +1.0% (38% won)   +1.7% (40% won)
#     15%     +4.0% (46%)       +5.0% (49%)
#     20%     +5.6%             +7.1%
# The wider stop wins because the tail pays: a 10% trail was shaken out of
# 39% of the +40% runs within a day of the entry, 15% out of 24%. Quant this
# week: 10% trail from the Thursday alert exited Friday +9%; 15% held to
# Sunday, +90%. In the calmer first half of the sample 10% and 15% tied;
# in the trending second half 15% won by five points. This is the hint for a
# NEW entry; stop_check.py still manages open positions on measured noise.
STOP_HINT_PCT = 15.0                   # the stop size the hint is sized for

# ALERT TIERS (2026-09-27), same 60-day study, first alert of the day only:
#     coin up <10% on the day AND volume >= 5x normal   +6.8% (10% trail), +11.1% (15%), 64% won, ~2/day
#     ...held in both 30-day halves (+6.6% / +7.0% at 10%)
#     coin up 20%+ on the WEEK (any alert)               -2.2% (10%), -3.2% (15%), 29% won
#     volume 2.5-5x, day <10%                           -1.3% (10%), +3.9% (15%)
# So a first alert on a quiet-week coin with heavy volume gets a star, and a
# coin that has already had its week gets a caution. The monsters (Quant +273%,
# USELESS +188%) DID come from the hot-week bucket - the caution is a statement
# about the average, not a ban, and the tags leave the decision on the phone.
TOP_TIER_MAX_DAY_PCT = 10.0
TOP_TIER_MIN_RELVOL = 5.0
# TOP SETUP is an alert only, not a buy signal (2026-10-16; claude/trading-
# strategy.md section 0). Its +11% came from one 60-day window to 09-27; in the
# month after, starred rate alerts averaged -5.3% under the 15% trail against
# +6.3% for random hours on the same coins (n=21, claude/alert-patterns-
# 2026-10-15.md). The replayed scheme never included it, and a short-window
# volume spike is the published pump-and-dump profile (claude/crypto-patterns-
# research-2026-10-15.md). It still goes to the quiet feed; True restores the
# old star, the overnight pass and the loud routing.
TOP_SETUP_IS_BUY = False
# A hot week (20%+) is information, not a red line, since 2026-10-16 (Kyle:
# "If i have rules that go against research take the research's approach").
# Weekly momentum is the best-replicated crypto pattern (Liu, Tsyvinski & Wu
# 2022 JF; Liu & Tsyvinski 2021 RFS; Fieberg et al. 2025 JFQA), and coins
# nearer their highs earn more (Jia et al. 2026 JBF). The one contrary study
# sorts on the biggest single day of the past month, not the week (Grobys &
# Junttila 2021). The old caution came from the scheme's own 60-day rate-alert
# sample (above). Sources: claude/crypto-patterns-research-2026-10-15.md and
# its round 2 (2026-10-16).
CAUTION_WEEK_PCT = 20.0
# LATE (2026-10-05 top-movers study, 150 days, 82 Robinhood coins): a coin
# already up 30%+ on the day went on to hit -15% before +20% in 63% of cases,
# with a median 7-day return of -17% (n=16 - small, so a warning, not a ban).
# Days of 15-30% were a coin flip (32% run first / 34% stop first).
LATE_DAY_PCT = 30.0

MARKET_TZ = "America/New_York"
MARKET_OPEN_HOUR = 8
MARKET_CLOSE_HOUR = 17

# A failed crypto screen is an outage, not a quiet market (2026-10-07). The
# 11:08 UTC run that day logged "crypto screener failed: HTTP Error 429" and
# then "tracking 0 liquid coins" / "daily context for 82 of 0 coins" / a
# watchlist line claiming BTC was not tradable - a run that reads as healthy
# and quiet. One 429 recovers on the next tick; a sustained one silences every
# crypto alert, so say so after this many consecutive failures (~5 min each).
SCREEN_FAIL_ALERT_RUNS = 3

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

# The quiet feed (2026-10-16). Same secret, so nothing new to set up; a name
# ntfy would refuse (over 64 characters) falls back to the main topic at low
# priority. Never log either name: this repository's logs are public.
NTFY_TOPIC_QUIET = (os.environ.get("NTFY_TOPIC_QUIET", "").strip()
                    or (NTFY_TOPIC + "-quiet" if NTFY_TOPIC else ""))
if len(NTFY_TOPIC_QUIET) > 64:
    NTFY_TOPIC_QUIET = ""
QUIET_PRIORITY = "low"
# Oct 19 2026: Kyle wants his phone to buzz only for patterns that passed a
# two-period backtest (pullback.py), plus alerts about money already in -
# stop checks - and the bot's own health. Everything else still goes out,
# to the quiet feed. Set False to restore the old behaviour.
LOUD_ONLY = True
# Mover pushes off (2026-10-19; Kyle: "i'm losing money" / "fix it" / "yes").
# Rate alerts, threshold crossings, dips turning up, unusual volume and the
# market-wide run / breadth summaries are attention triggers: the pushes about
# movers that raise retail risk-taking (Arnold, Pelster & Subrahmanyam, JFE
# 2022), and they fire in exactly the hours the ledger's buys outside the plan
# were made. A market-wide bounce entry is 2-3 points a trade worse than a
# random hour in both halves of the past 14 months
# (claude/bounce-study-2026-10-18.md); the dip push ran -10% a trade in
# October (claude/alert-patterns-2026-10-15.md). With this False they are
# still screened, logged ("[off] ...") and remembered (fired state, the
# per-day tally, TOP SETUP bookkeeping), but nothing is sent, not even to the
# quiet feed. The gold star, the overnight digest, the breakout "not a buy"
# notes, the stop warnings, bot health and the pullback alerts are unaffected.
# True restores them (they then follow LOUD_ONLY as before).
MOVER_PUSHES = False

# The book and the standing rules (claude/trading-strategy.md, section 0).
POSITIONS_FILE = Path(os.environ.get("POSITIONS_FILE", "positions.json"))
BUY_START_HOUR = 7          # rule 1: nothing bought 7 PM - 7 AM Central
BUY_END_HOUR = 19
DEFAULT_LOT_USD = 500
# Slots are coins, not lots (2026-10-19): the replays that size the scheme
# hold one position per coin, and the add rule bounds the lots of one coin.
# Ten slots kept 95% of the ten-year replay's money with two thirds of the
# trades and a shallower worst stretch (claude/full-history-replay-2026-10-19.md).
DEFAULT_MAX_COINS = 10
DEFAULT_CRYPTO_CAP_USD = 40_000
NIGHT_DIGEST_MAX_AGE_H = 14
# A gold star is re-sent as "still a buy" this many minutes after it went out
# (each entry once), while the coin is still a buy and not yet in
# positions.json. Kyle's phone never received the PEPE star of 2026-10-20
# although ntfy had accepted it; a second, independent push covers a dropped
# one. Stars go at ntfy's "max" priority for the same reason (2026-10-21).
STAR_REPEAT_MIN = (10, 30)
STAR_PRIORITY = "max"

# Stop watch. A triggered Robinhood crypto stop is a market sell collared 5%
# under the stop; past that it cannot fill and may be canceled after five
# minutes (Robinhood Crypto glossary, checked 2026-10-15).
COLLAR_PCT = 5.0
COLLAR_MARGIN_PCT = 1.0     # Coinbase vs Robinhood quote gap
CRASH_MIN_COINS = 3
STOP_WATCH_COOLDOWN_SEC = 6 * 3600

# Venue watch (2026-10-25, rulebook section 9). A stop is only protection
# while the venue can trade, and Robinhood's own status page is the earliest
# public word that it cannot. Both pages are Atlassian Statuspage sites:
# /api/v2/status.json gives an overall indicator (none, minor, major,
# critical), /api/v2/incidents/unresolved.json the open incidents, and
# /api/v2/components.json per-component states (empty on Robinhood's page;
# Coinbase's has about 180, the ones that matter here named below).
# Robinhood: a major/critical indicator, or an open incident whose name has
# one of VENUE_WORDS in it, is a max-priority push while positions.json holds
# any lot - once per incident, again every VENUE_COOLDOWN_SEC while it stays
# open, and a quiet note when it clears; other Robinhood incidents, and
# anything while nothing is held, go to the quiet feed. Coinbase is where the
# bot's prices and the stop checks come from, so a non-operational trading or
# API component, or a major incident, is a quiet note: it means the bot may be
# working from stale prices, not that Kyle's orders are at risk. A status
# page that cannot be read is logged and nothing more - that is not a venue
# outage.
STATUS_PAGES = {"Robinhood": "https://status.robinhood.com",
                "Coinbase": "https://status.coinbase.com"}
VENUE_WORDS = ("crypto", "trading", "trade", "order", "stop", "outage")
COINBASE_COMPONENTS = ("Trading", "Coinbase - Advanced Trade", "Coinbase - API")
VENUE_COOLDOWN_SEC = 6 * 3600

# Peg watch (2026-10-25, rulebook section 9). A stablecoin depeg is the one
# kind of news with a measured, immediate effect on bitcoin: a USDT move past
# 0.5% from $1 raises the odds of a BTC jump in the next five minutes about
# fivefold (Perez Riaza & Gnabo 2025, J. Int. Money & Finance). Information,
# not a rule - the red lines are frozen until the Nov 5 review - so the push
# says what it is and what to check. USDC and USDT are priced on Kraken's USD
# pairs (deep USD markets for both; Coinbase has no USDC-USD product, checked
# 2026-10-25). A coin DEPEG_PCT or more off the dollar is a max-priority push
# once per episode, again if the gap widens by another DEPEG_PCT or after
# DEPEG_COOLDOWN_SEC, and a quiet note once it is back within REPEG_PCT.
PEG_URL = "https://api.kraken.com/0/public/Ticker?pair=USDCUSD,USDTUSD"
PEG_COINS = ("USDC", "USDT")
DEPEG_PCT = 0.5
REPEG_PCT = 0.2
DEPEG_COOLDOWN_SEC = 6 * 3600

PUSH_RETRY_WAIT_S = 3
PUSH_STATS = {"failed": 0}

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&order=market_cap_desc"
    f"&per_page={CRYPTO_TOP_N}&page=1&price_change_percentage=1h,24h,7d"
)
# Same fields, for named symbols. include_tokens=top returns the largest coin
# for each symbol, which is the one Robinhood lists.
COINGECKO_SYMBOLS_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&include_tokens=top&price_change_percentage=1h,24h,7d"
    "&symbols="
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
         click=None, quiet=False, loud=False, mover=False):
    """Send one notification. Headers must be ASCII; the body may be UTF-8.

    quiet=True sends it to the quiet feed at low priority. mover=True marks a
    push about a mover (rate, threshold, dip, volume, market-wide run): with
    MOVER_PUSHES False it is logged and dropped, and the call returns True so
    the caller's bookkeeping (fired state, the day's tally) stays as if it
    had gone. Returns True once ntfy accepts it and False after two failed
    tries; a failure is counted and main() then exits non-zero, so the run
    shows red.
    """
    import time
    import urllib.request

    if mover and not MOVER_PUSHES:
        log(f"  [off] {title}")
        return True
    topic = NTFY_TOPIC
    if LOUD_ONLY and not loud and tags != BUY_TAG:
        quiet = True        # Kyle, Oct 19 2026: only tested signals and gold-star buys buzz
    if quiet:
        priority = QUIET_PRIORITY
        topic = NTFY_TOPIC_QUIET or NTFY_TOPIC
    if not topic:
        log("NTFY_TOPIC is not set - printing instead of sending")
        log(f"  {'[quiet] ' if quiet else ''}{title} | {message}")
        return True

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
        f"{NTFY_SERVER}/{topic}",
        data=str(message).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status < 300:
                    return True
                log(f"  ntfy returned {resp.status} (try {attempt})")
        except Exception as exc:
            log(f"  push failed (try {attempt}): {exc}")
        if attempt == 1:
            time.sleep(PUSH_RETRY_WAIT_S)
    PUSH_STATS["failed"] += 1
    return False


# --- The standing rules: which alert may buzz ----------------------------------

def load_book():
    """positions.json as a dict, or None if it cannot be read."""
    try:
        return json.loads(POSITIONS_FILE.read_text())
    except Exception as exc:
        log(f"  positions file unreadable ({exc}) - buy alerts go to the quiet feed")
        return None


def base_symbol(sym):
    """'PYTH#3' -> 'PYTH'."""
    return str(sym or "").split("#", 1)[0].upper()


def lot_number(sym):
    """'PYTH' is lot 1, 'PYTH#3' lot 3."""
    s = str(sym or "")
    if "#" not in s:
        return 1
    try:
        return int(s.split("#", 1)[1])
    except ValueError:
        return 99


def lot_breakeven(p):
    """Same arithmetic as stop_check.breakeven_stop, unrounded."""
    fee = 0.0 if p.get("routing") == "market_maker" else 0.95
    return float(p["avg_cost"]) / (1 - (fee + 0.2) / 100.0)


def local_at(now_ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now_ts, ZoneInfo(USER_TZ))
    except Exception:
        return None


def in_buy_window(now_ts):
    """Rule 1: buys only from 7 AM to 7 PM Central."""
    local = local_at(now_ts)
    if local is None:
        return True          # a zone lookup failure must not silence buys
    return BUY_START_HOUR <= local.hour < BUY_END_HOUR


# The gold star (2026-10-16): one unmistakable buy signal on the phone. ntfy
# shows a tag that names an emoji in front of the title, so the star is
# reserved for one thing: a buy signal (BASE BREAKOUT, or TOP SETUP while
# TOP_SETUP_IS_BUY) with no red line, that every standing rule allows right
# now. Only that alert is loud, and its title starts with the coin, "BUY" and
# the size. The same setup when a rule says no carries one of the other two
# icons and goes to the quiet feed. No other alert uses the star.
# The crash budget (2026-10-16, round-2 research). A stop is not a cap on
# the loss: in a cascade the price jumps through the stop and its 5% collar
# on every coin at once (Scaillet, Treccani & Trevisan 2020 JFEc; Saef et al.
# 2024; Ji et al. 2019 IRFA), and on Oct 10, 2025 the coins held now fell
# 7-75% below where their stops would have been. So a buy also has to fit a
# crash budget: the loss against cost if every coin fell as far below its
# stop as it did that day, new lot included, within "_crash_budget_usd"
# (positions.json; $11,500 + realized P&L since 10-05, kept current by
# Claude). Each coin's gap is in crash_gaps.json; a coin not there uses
# its "_default" (the median gap of the coins held on 2026-10-16). Source:
# claude/backdoor-review-2026-10-15.md section 3 and the round-2 research.
CRASH_GAPS_FILE = Path(os.environ.get("CRASH_GAPS_FILE", "crash_gaps.json"))
DEFAULT_CRASH_GAP = 0.21
# The memecoin pack counts as one coin (2026-10-18; Kyle: "If you think it'll
# help us make money then yes"). Sub-penny memecoins move together (0.63 daily
# correlation with each other against 0.49 with the rest: strategy section 3),
# so a second memecoin is an add to the first, not a new position, and under
# the add rule it never gets the star. Edit the list as Robinhood's lineup
# changes; a coin not listed is treated as its own coin.
MEME_PACK = {
    "DOGE", "SHIB", "PEPE", "BONK", "FLOKI", "WIF", "PENGU", "MEW", "TRUMP", "MELANIA",
    "FARTCOIN", "PNUT", "MOODENG", "POPCAT", "BRETT", "TURBO", "CASHCAT", "PONKE",
    "GOAT", "NEIRO", "BOME", "DOG", "MOG", "SPX", "GIGA", "TOSHI", "WOJAK", "BABYDOGE",
    "MYRO", "SLERF", "PUMP",
}
NEW_LOT_STOP_ROOM = 0.15                 # a new lot's stop sits 15% under the fill

BUY_TAG = "star"                         # gold star: buy now
WAIT_TAG = "hourglass_flowing_sand"      # outside 7 AM - 7 PM: the 7 AM run re-checks it
NO_BUY_TAG = "no_entry_sign"             # a rule or a red line says no


def buy_decision(symbol, now_ts, book=None, pending=None, half=None):
    """May a buy signal on `symbol` be bought right now? (ok, note, usd).

    The checks are the standing rules (claude/trading-strategy.md): the
    7 AM - 7 PM window, the "_buys_allowed" switch (off while open risk is
    over the limit), at most "_max_open_coins" coins held at once (a coin's
    lots count once; a star for a coin not yet held needs a free slot), open
    crypto cost plus one lot within "_crypto_cost_cap_usd" (no new margin),
    the crash budget when "_crash_budget_usd" is set (see CRASH_GAPS_FILE),
    and no alert buy of a coin already held: under the add rule an add comes
    only from a calm stop check (the coin up under 5% on the day), which a
    breakout day never is. The memecoin pack (MEME_PACK) counts as one coin
    for that rule.
    `half` names a yellow flag that halves the lot (a high-vol coin).
    `pending` lists the buys already starred earlier in the same run
    ({"symbol", "usd"}); they count as bought, so two stars in one run cannot
    overrun the book together. Buys made since positions.json was last
    updated are not known here. The note becomes part of the title: the size
    when it passes, the reason when it does not. usd is the size to buy, 0
    when the answer is no.
    """
    if not in_buy_window(now_ts):
        return False, "outside 7 AM-7 PM CT: no buys", 0.0
    if book is None:
        book = load_book()
    if not book:
        return False, "positions file unreadable", 0.0
    if book.get("_buys_allowed") is False:
        return False, str(book.get("_buys_note") or "buys paused"), 0.0
    lots = book.get("positions") or []
    extra = [p for p in pending or [] if isinstance(p, dict)]
    sym = base_symbol(symbol)
    coins = {base_symbol(p.get("symbol")) for p in lots}
    starred = {base_symbol(p.get("symbol")) for p in extra}
    max_coins = int(book.get("_max_open_coins") or DEFAULT_MAX_COINS)
    if sym not in coins and len(coins | starred) >= max_coins:
        if starred - coins:
            return (False, f"{len(coins)} coins held + {len(starred - coins)} starred "
                           f"this run, max {max_coins}", 0.0)
        return False, f"{len(coins)} coins held, max {max_coins}", 0.0
    lot = float(book.get("_lot_size_usd") or DEFAULT_LOT_USD)
    cost = (sum(float(p["qty"]) * float(p["avg_cost"]) for p in lots)
            + sum(float(p.get("usd") or 0) for p in extra))
    cap = float(book.get("_crypto_cost_cap_usd") or DEFAULT_CRYPTO_CAP_USD)
    if cost + lot > cap:
        return False, f"crypto cost ${cost:,.0f} is at the ${cap:,.0f} cap", 0.0
    if any(base_symbol(p.get("symbol")) == sym for p in extra):
        return False, "already starred this run", 0.0
    held = [p for p in lots if base_symbol(p.get("symbol")) == sym]
    if not held and sym in MEME_PACK:
        pack = sorted({base_symbol(p.get("symbol")) for p in lots
                       if base_symbol(p.get("symbol")) in MEME_PACK})
        if pack:
            return (False, f"memecoin pack already held ({', '.join(pack)}): "
                           f"counts as one coin", 0.0)
        if any(base_symbol(p.get("symbol")) in MEME_PACK for p in extra):
            return False, "memecoin pack already starred this run", 0.0
    if held:
        local = local_at(now_ts)
        today = (local or datetime.fromtimestamp(now_ts, timezone.utc)).date().isoformat()
        if any(str(p.get("bought")) == today for p in held):
            return False, "already bought today", 0.0
        first = min(held, key=lambda p: lot_number(p.get("symbol")))
        stop = first.get("current_stop")
        if stop is None or float(stop) < lot_breakeven(first):
            return False, "held, first lot not locked: no add", 0.0
        return False, "held: adds only at a calm stop check", 0.0
    size = lot / 2 if half else lot
    budget = book.get("_crash_budget_usd")
    if budget is not None:
        try:
            gaps = load_crash_gaps()
            total = crash_case(lots, gaps, extra) + crash_loss_new(size, sym, gaps)
            budget = float(budget)
        except (TypeError, ValueError, KeyError) as exc:
            # A risk limit that cannot be read says no, not yes.
            return False, f"crash budget check failed ({exc})", 0.0
        if total > budget:
            return (False, f"over the crash budget (${total:,.0f} vs "
                           f"${budget:,.0f})", 0.0)
    if half:
        return True, f"${size:,.0f} lot (half size: {half})", size
    return True, f"${size:,.0f} lot", size


def load_crash_gaps():
    """crash_gaps.json as {SYMBOL: fraction}, or {} if it cannot be read."""
    try:
        data = json.loads(CRASH_GAPS_FILE.read_text())
    except Exception as exc:
        log(f"  crash gaps unreadable ({exc}) - every coin gets the "
            f"{DEFAULT_CRASH_GAP:.0%} default")
        return {}
    return {(k if str(k).startswith("_") else str(k).upper()): v
            for k, v in (data.items() if isinstance(data, dict) else [])}


def crash_gap(gaps, symbol):
    """How far below its stop this coin fell on Oct 10, 2025 (0-0.95)."""
    g = (gaps or {}).get(base_symbol(symbol))
    if not isinstance(g, (int, float)):
        g = (gaps or {}).get("_default", DEFAULT_CRASH_GAP)
    try:
        return min(max(float(g), 0.0), 0.95)
    except (TypeError, ValueError):
        return DEFAULT_CRASH_GAP


def crash_loss_new(usd, symbol, gaps):
    """The crash loss of a new lot: its 15% stop, then that coin's gap."""
    return float(usd) * (1 - (1 - NEW_LOT_STOP_ROOM) * (1 - crash_gap(gaps, symbol)))


def crash_case(lots, gaps, pending=()):
    """Loss against cost if every coin fell as far below its stop as it did
    on Oct 10, 2025. A lot without a stop is taken at 15% under its cost.
    Stars sent earlier in the run count as new lots."""
    loss = 0.0
    for p in lots or []:
        qty, cost = float(p["qty"]), float(p["avg_cost"])
        stop = p.get("current_stop")
        stop = float(stop) if stop is not None else cost * (1 - NEW_LOT_STOP_ROOM)
        loss += qty * cost - qty * stop * (1 - crash_gap(gaps, p.get("symbol")))
    for p in pending or []:
        loss += crash_loss_new(p.get("usd") or 0, p.get("symbol"), gaps)
    return loss


def buy_gate(symbol, now_ts, book=None, pending=None, half=None):
    """buy_decision without the size: (ok, note)."""
    ok, note, _ = buy_decision(symbol, now_ts, book, pending, half)
    return ok, note


# The red list (strategy section 1) as the alert lines print it. Any one of
# them means no buy, so no star. The hot week left it on 2026-10-16
# (CAUTION_WEEK_PCT).
RED_PREFIXES = ("RED", "LATE", "no catch-up")


def first_red(lines):
    """The first line that takes the star away, cut to its headline, or None."""
    for ln in lines:
        s = str(ln)
        if s.startswith(RED_PREFIXES):
            return s.split(" - ", 1)[0]
    return None


def red_lines(lines):
    """True if the alert carries a line that takes the star away."""
    return first_red(lines) is not None


def half_reason(lines):
    """The yellow flag that halves the lot (strategy section 1), or None."""
    return "high-vol coin" if any(str(ln).startswith("high-vol coin") for ln in lines) else None


def remember_night_star(state, r, what, now_ts, kind="base"):
    """Keep a buy signal that arrived outside the buy window for the 7 AM run."""
    if state is None:
        return
    stars = state.setdefault("night_stars", [])
    if any(s.get("symbol") == r["symbol"] for s in stars):
        return
    stars.append({"symbol": r["symbol"], "what": what, "kind": kind,
                  "price": r.get("price"), "ts": now_ts})
    del stars[:-20]


def buy_verdict(r, what, now_ts, state=None, kind="base", lines=()):
    """What the phone shows for a buy signal: a dict with quiet, title, tags,
    head (the first body line) and usd (the size, 0 unless it is a buy).

    `lines` is the alert body so far, where the red lines and the half-size
    flag are read. The gold star goes only to a signal with no red line that
    the standing rules allow right now. One that arrives outside 7 AM - 7 PM
    is kept for the first run inside the window, which re-checks it
    (send_night_digest).
    """
    sym = r["symbol"]
    red = first_red(lines)
    if red:
        return {"quiet": True, "tags": NO_BUY_TAG, "usd": 0.0,
                "title": f"{sym} - not a buy - {what} ({red})",
                "head": f"NOT A BUY: {red}"}
    if r.get("kind") != "crypto":
        return {"quiet": True, "tags": "zap", "usd": 0.0,
                "title": f"{sym} {what}", "head": "NOT A BUY: stocks are not in the scheme"}
    ok, note, usd = buy_decision(sym, now_ts, pending=(state or {}).get("run_stars"),
                                 half=half_reason(lines))
    if ok:
        return {"quiet": False, "tags": BUY_TAG, "usd": usd,
                "title": f"{sym} - BUY {note} - {what}",
                "head": buy_head(note, usd)}
    if not in_buy_window(now_ts):
        remember_night_star(state, r, what, now_ts, kind)
        return {"quiet": True, "tags": WAIT_TAG, "usd": 0.0,
                "title": f"{sym} - not a buy yet - {what} (7 AM re-check)",
                "head": ("NOT A BUY YET: outside 7 AM-7 PM CT. The 7 AM run re-checks it "
                         "and sends a gold star only if it is still a buy.")}
    return {"quiet": True, "tags": NO_BUY_TAG, "usd": 0.0,
            "title": f"{sym} - not a buy - {what} ({note})",
            "head": f"NOT A BUY: {note}"}


def note_star(state, r, v, now_ts=None, what=None):
    """Count a sent gold star against the book for the rest of the run, and
    remember it for the "still a buy" re-sends (send_star_repeats)."""
    if state is not None and v.get("usd"):
        state.setdefault("run_stars", []).append(
            {"symbol": base_symbol(r["symbol"]), "usd": float(v["usd"])})
        if now_ts is not None:
            sym = base_symbol(r["symbol"])
            stars = [s for s in state.get("open_stars") or [] if s.get("symbol") != sym]
            stars.append({"symbol": sym, "ts": float(now_ts), "usd": float(v["usd"]),
                          "what": what or "base breakout", "repeats": 0})
            state["open_stars"] = stars


def send_star_repeats(state, rows, now_ts, btc_price=None, book=None):
    """Re-send a gold star as STILL A BUY at STAR_REPEAT_MIN minutes after it
    went out, once per entry, while the coin is still a buy right now (a
    green base breakout, no red line, the book has room: morning_check) and
    is not yet in positions.json. A coin that is bought, no longer a buy, or
    past the last repeat is forgotten; nothing carries past 7 PM."""
    if state is None or not state.get("open_stars"):
        return
    if not in_buy_window(now_ts):
        state["open_stars"] = []
        return
    book = book if book is not None else load_book()
    held = {base_symbol(p.get("symbol")) for p in (book or {}).get("positions") or []}
    by_sym = {str(r.get("symbol") or "").upper(): r for r in rows or []}
    keep = []
    for s in state["open_stars"]:
        sym = str(s.get("symbol") or "").upper()
        n = int(s.get("repeats") or 0)
        if sym in held:
            log(f"  star repeat: {sym} is in the book - done")
            continue
        if n >= len(STAR_REPEAT_MIN):
            continue
        if now_ts < float(s.get("ts") or 0) + STAR_REPEAT_MIN[n] * 60:
            keep.append(s)
            continue
        row = by_sym.get(sym)
        ok, note, usd = morning_check(state, row, btc_price, now_ts,
                                      pending=state.get("run_stars"))
        if not ok:
            log(f"  star repeat: {sym} no longer a buy ({note}) - let it go")
            continue
        local = local_at(float(s["ts"]))
        when = local.strftime("%I:%M %p").lstrip("0") + " CT" if local else "earlier"
        day = ""
        try:
            ctx = dc.coin_context((state.get("daily") or {}).get(sym), row["price"],
                                  dc.utc_day(now_ts))
            if ctx:
                day = f" (+{ctx['day_pct']:.1f}% today)"
        except Exception:
            pass
        breakeven, stop = cost_hints(row["price"])
        last = n + 1 >= len(STAR_REPEAT_MIN)
        body = [f"STILL A BUY: the gold star for {sym} went out at {when} and {sym} is not "
                f"in positions.json. " + buy_head(note, usd),
                f"{row.get('name') or sym}  -  {money(row['price'])}",
                f"re-checked now: still a base breakout{day}, no red line, the book has room",
                f"break-even {money(breakeven)}  -  {STOP_HINT_PCT:.0f}% stop {money(stop)}",
                "Last reminder." if last else
                f"One more reminder at {STAR_REPEAT_MIN[-1]} minutes if it is still a buy."]
        ok_push = push(f"{sym} - BUY {note} - still a buy ({s.get('what') or 'base breakout'}), "
                       f"star sent {when}",
                       "\n".join(body), priority=STAR_PRIORITY, tags=BUY_TAG,
                       click=robinhood_url(row), quiet=False)
        if ok_push is False:
            keep.append(s)                       # try again next run
            continue
        s["repeats"] = n + 1
        log(f"  STILL A BUY {sym}: reminder {n + 1} of {len(STAR_REPEAT_MIN)}, "
            f"star sent {when}")
        if not last:
            keep.append(s)
    state["open_stars"] = keep


def morning_check(state, row, btc_price, now_ts, pending=None):
    """Is an overnight signal a buy at the first run inside the window?
    (ok, note, usd). The live alert's own tests on the live price: a green
    BASE BREAKOUT, no red line, then the book's limits, with the half-size
    flag. A TOP SETUP kept overnight passes only if it is a base breakout by
    then: its rate trigger is hours old and cannot be re-checked."""
    if not row or not row.get("price"):
        return False, "no live price", 0.0
    if dc is None:
        return False, "no daily context", 0.0
    sym = str(row.get("symbol") or "").upper()
    try:
        ctx = dc.coin_context((state.get("daily") or {}).get(sym), row["price"],
                              dc.utc_day(now_ts))
        green, why = dc.is_breakout(ctx, now_ts)
        lines = tier_lines(row, None, False)[1] + dc.context_lines(state, row, btc_price, now_ts)
    except Exception as exc:
        return False, f"re-check failed ({exc})", 0.0
    if not green:
        return False, f"not a base breakout now ({why})", 0.0
    red = first_red(lines)
    if red:
        return False, red, 0.0
    return buy_decision(sym, now_ts, pending=pending, half=half_reason(lines))


def send_night_digest(state, rows, now_ts, btc_price=None):
    """Rule 1: a buy signal that arrives outside 7 AM - 7 PM waits for the
    first run inside the window, which re-checks each one on the live price
    (morning_check). Each one still a buy gets its own gold-star alert, loud,
    with a tap through to its Robinhood page. The rest go to the quiet feed in
    one list, each with its reason. Nothing from the night gets a star
    without that re-check."""
    if state is None or not state.get("night_stars") or not in_buy_window(now_ts):
        return
    fresh = [s for s in state["night_stars"]
             if now_ts - float(s.get("ts") or 0) <= NIGHT_DIGEST_MAX_AGE_H * 3600]
    state["night_stars"] = []
    if not fresh:
        return
    by_sym = {str(r.get("symbol") or "").upper(): r for r in rows or []}
    retry, rest, bought = [], [], []
    for s in fresh:
        sym = str(s.get("symbol") or "").upper()
        row = by_sym.get(sym)
        then, now_p = s.get("price"), (row or {}).get("price")
        local = local_at(float(s.get("ts") or now_ts))
        when = local.strftime("%I:%M %p").lstrip("0") if local else "overnight"
        what = s.get("what") or ("base breakout" if s.get("kind", "base") == "base" else "setup")
        move = (f", now {money(now_p)} ({(now_p / then - 1) * 100:+.1f}%)"
                if now_p and then else "")
        seen = f"{what} at {when}, {money(then)}{move}"
        ok, note, usd = morning_check(state, row, btc_price, now_ts,
                                      pending=state.get("run_stars"))
        if not ok:
            rest.append((s, f"{sym}: {seen} - {note}"))
            continue
        breakeven, stop = cost_hints(now_p)
        body = [buy_head(note, usd),
                f"{row.get('name') or sym}  -  {money(now_p)}",
                f"overnight: {seen}",
                "re-checked now: still a base breakout, no red line, the book has room",
                f"break-even {money(breakeven)}  -  {STOP_HINT_PCT:.0f}% stop {money(stop)}"]
        if push(f"{sym} - BUY {note} - overnight breakout, still a buy",
                "\n".join(body), priority=STAR_PRIORITY, tags=BUY_TAG,
                click=robinhood_url(row), quiet=False) is False:
            retry.append(s)                   # try again on the next run
        else:
            note_star(state, row, {"usd": usd}, now_ts, "overnight breakout")
            bought.append(sym)
    if rest:
        title = ("Overnight setups, not a buy now: "
                 + ", ".join(str(s.get("symbol") or "?") for s, _ in rest))
        lines = [ln for _, ln in rest]
        lines.append("No gold star = no buy. Let these go.")
        if push(title, "\n".join(lines), priority=QUIET_PRIORITY, tags=NO_BUY_TAG,
                quiet=True) is False:
            retry.extend(s for s, _ in rest)
    state["night_stars"] = retry
    log(f"  NIGHT DIGEST {len(fresh)} overnight signal(s): "
        f"{len(bought)} still a buy{' (' + ', '.join(bought) + ')' if bought else ''}, "
        f"{len(rest)} not" + (f", {len(retry)} kept to resend" if retry else ""))


def coinbase_price(product, timeout=6):
    """Coinbase's last trade for one product, or None."""
    import urllib.request
    req = urllib.request.Request(f"{COINBASE_API}/products/{product}/ticker",
                                 headers={"User-Agent": "market-movers-alerts/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        price = float(json.loads(resp.read().decode("utf-8")).get("price") or 0)
    return price if price > 0 else None


def check_live_stops(state, now_ts, book=None, price_of=None):
    """Loud "check your stops" for held coins (2026-10-16).

    Robinhood turns a triggered crypto stop into a market sell collared 5%
    under the stop price. If the coin trades below that, the sell cannot fill
    and Robinhood may cancel it after five minutes, which leaves the lot
    unprotected and nothing in positions.json would know. So a held coin
    under that limit gets a max-priority push, as does a book where several
    coins are at their stops at once. Six hours between repeats per coin.
    """
    book = book if book is not None else load_book()
    if not book or state is None:
        return
    price_of = price_of or coinbase_price
    lots = [p for p in book.get("positions") or [] if p.get("current_stop") is not None]
    if not lots:
        return
    watch = state.setdefault("stop_watch", {})
    prices = {}
    for p in lots:
        prod = p.get("product")
        if prod and prod not in prices:
            try:
                prices[prod] = price_of(prod)
            except Exception as exc:
                log(f"  stop watch: no price for {prod} ({exc})")
                prices[prod] = None
    under, at_stop = {}, {}
    for p in lots:
        px = prices.get(p.get("product"))
        if not px:
            continue
        stop = float(p["current_stop"])
        coin = base_symbol(p.get("symbol"))
        limit = stop * (1 - COLLAR_PCT / 100.0)
        if px <= stop:
            at_stop[coin] = px
        if px < limit * (1 - COLLAR_MARGIN_PCT / 100.0):
            under.setdefault(coin, []).append((p.get("symbol"), stop, limit, px))
    for coin, hits in sorted(under.items()):
        if now_ts - float(watch.get(coin) or 0) < STOP_WATCH_COOLDOWN_SEC:
            continue
        px = hits[0][3]
        body = [f"{coin} is at {money(px)}, under the sell limit of "
                f"{len(hits)} stop{'s' if len(hits) > 1 else ''}:"]
        for sym, stop, limit, _ in hits:
            body.append(f"  {sym}: stop {money(stop)}, sells no lower than {money(limit)}")
        body.append("A triggered stop cannot sell this far down, and Robinhood "
                    "may cancel it if it does not fill within 5 minutes. Open "
                    "Robinhood: is each stop still open, filled or canceled?")
        if push(f"CHECK STOPS: {coin} under its stop limit", "\n".join(body),
                priority="max", tags="rotating_light",
                click=f"https://robinhood.com/crypto/{coin}", loud=True) is not False:
            watch[coin] = now_ts
        log(f"  STOP WATCH {coin} {money(px)} under the limit of {len(hits)} stop(s)")
    if (len(at_stop) >= CRASH_MIN_COINS
            and now_ts - float(watch.get("_crash") or 0) >= STOP_WATCH_COOLDOWN_SEC):
        coins = sorted(at_stop)
        if push(f"CRASH CHECK: {len(coins)} coins at their stops",
                f"{', '.join(coins)} are at or under their stop prices, so those "
                f"stops should be filling. Check that each one filled or is "
                f"still open, and report the fills so the records match.",
                priority="max", tags="rotating_light", loud=True) is not False:
            watch["_crash"] = now_ts
        log(f"  CRASH CHECK {len(coins)} coins at their stops: {', '.join(coins)}")


# --- Venue status and stablecoin pegs (2026-10-25) --------------------------

def fetch_json(url, timeout=6):
    """One JSON document over HTTPS, or the exception."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "market-movers-alerts/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def status_page(base, timeout=6):
    """A Statuspage-hosted status page, reduced to what the venue watch needs:
    {"indicator", "description", "incidents": [{id, name, status, impact}],
    "components": {name: status}}. Raises if the overall status or the open
    incidents cannot be read; a missing component list is just empty."""
    status = fetch_json(f"{base}/api/v2/status.json", timeout)
    unresolved = fetch_json(f"{base}/api/v2/incidents/unresolved.json", timeout)
    try:
        comps = fetch_json(f"{base}/api/v2/components.json", timeout)
    except Exception:
        comps = {}
    st = status.get("status") or {}
    return {
        "indicator": str(st.get("indicator") or "none").lower(),
        "description": str(st.get("description") or ""),
        "incidents": [{"id": str(i.get("id") or i.get("shortlink") or i.get("name") or ""),
                       "name": str(i.get("name") or ""),
                       "status": str(i.get("status") or ""),
                       "impact": str(i.get("impact") or "").lower()}
                      for i in (unresolved.get("incidents") or [])],
        "components": {str(c.get("name") or ""): str(c.get("status") or "").lower()
                       for c in (comps.get("components") or [])},
    }


def venue_problems(venue, page):
    """What on one status page deserves a word: [(key, what, serious)].

    serious marks the Robinhood items that buzz while lots are held - a
    major or critical indicator, or an open incident about crypto, trading,
    orders, stops or an outage (VENUE_WORDS), or one Robinhood itself rates
    major or critical. Other Robinhood incidents are listed as quiet. On
    Coinbase only the trading and API components (COINBASE_COMPONENTS) and
    major incidents count, all quiet: Coinbase's page carries a few minor
    incidents most days (payment rails, one chain's sends) that mean nothing
    here.
    """
    out = []
    ind = page.get("indicator") or "none"
    bad = ind in ("major", "critical")
    if venue == "Robinhood":
        if bad:
            out.append(("indicator", f"status page: {page.get('description') or ind}", True))
        for inc in page.get("incidents") or []:
            name = inc.get("name") or ""
            hot = (any(w in name.lower() for w in VENUE_WORDS)
                   or inc.get("impact") in ("major", "critical"))
            out.append((f"incident:{inc.get('id')}",
                        f"{name} ({inc.get('status') or '?'}, impact {inc.get('impact') or '?'})",
                        hot))
        return out
    comps = page.get("components") or {}
    for name in COINBASE_COMPONENTS:
        st = comps.get(name)
        if st and st != "operational":
            out.append((f"component:{name}", f"{name}: {st.replace('_', ' ')}", False))
    for inc in page.get("incidents") or []:
        if inc.get("impact") in ("major", "critical"):
            out.append((f"incident:{inc.get('id')}",
                        f"{inc.get('name')} ({inc.get('status') or '?'}, impact {inc.get('impact')})",
                        False))
    if bad:
        out.append(("indicator", f"status page: {page.get('description') or ind}", False))
    return out


def check_venues(state, now_ts, book=None, fetch=None):
    """Venue watch (2026-10-25): a loud push when Robinhood's status page
    reports trouble with trading while lots are held.

    Every run reads STATUS_PAGES. A serious Robinhood item (venue_problems)
    is a max-priority push while positions.json holds any lot, once per
    item and again every VENUE_COOLDOWN_SEC while it stays listed, with a
    quiet note when it clears; everything else that qualifies goes to the
    quiet feed on the same cadence. state["venue"] remembers what has been
    said, keyed by venue and incident, and survives the daily reset. A page
    that cannot be read is logged and skipped - a status page outage is not
    a venue outage, and nothing is marked cleared on a failed read.
    """
    if state is None:
        return
    book = book if book is not None else load_book()
    lots = (book or {}).get("positions") or []
    coins = sorted({base_symbol(p.get("symbol")) for p in lots if p.get("symbol")})
    fetch = fetch or status_page
    seen = state.setdefault("venue", {})
    summary = []
    for venue, base in STATUS_PAGES.items():
        try:
            page = fetch(base)
        except Exception as exc:
            log(f"  venue watch: {venue} status page unreadable ({exc})")
            summary.append(f"{venue} unreadable")
            continue
        problems = venue_problems(venue, page)
        summary.append(f"{venue} {page.get('indicator') or 'none'}"
                       + (f" ({len(problems)} to note)" if problems else ""))
        live_keys = {f"{venue}:{k}" for k, _, _ in problems}
        for key in [k for k in list(seen) if k.startswith(venue + ":") and k not in live_keys]:
            prev = seen.pop(key)
            what = prev.get("what") if isinstance(prev, dict) else key
            if isinstance(prev, dict) and prev.get("loud"):
                push(f"{venue} status: cleared - {what}",
                     f"{venue}'s status page no longer lists it. Check that each stop "
                     f"you hold is still open and at its price: an order touched during "
                     f"the incident may have filled or been canceled.",
                     tags="white_check_mark", click=base, quiet=True)
            log(f"  VENUE {venue} cleared: {what}")
        for k, what, serious in problems:
            key = f"{venue}:{k}"
            prev = seen.get(key) or {}
            if now_ts - float(prev.get("t") or 0) < VENUE_COOLDOWN_SEC:
                continue
            loud = bool(serious and lots)
            if loud:
                body = [f"Robinhood reports: {what}",
                        f"Status page: {page.get('description') or page.get('indicator')}",
                        f"You hold {len(lots)} lot{'s' if len(lots) != 1 else ''} on "
                        f"{len(coins)} coin{'s' if len(coins) != 1 else ''}: {', '.join(coins)}.",
                        "A stop is only protection while Robinhood can trade. As soon as the "
                        "app works: check that each stop is still open and at its price, and "
                        "report any fill or cancel so the records match. A gold star during "
                        "an outage cannot be filled or protected - let it go.",
                        f"Repeats every {VENUE_COOLDOWN_SEC // 3600} hours while it stays open."]
                ok = push(f"ROBINHOOD STATUS: {what}", "\n".join(body), priority="max",
                          tags="rotating_light", click=base, loud=True)
            elif venue == "Robinhood":
                ok = push(f"Robinhood status: {what}",
                          f"Robinhood's status page lists: {what}. "
                          + ("Nothing is held, so this is for information."
                             if not lots else
                             "Not about trading as far as the name says; stops are unaffected "
                             "unless the app itself is down."),
                          tags="warning", click=base, quiet=True)
            else:
                ok = push(f"Coinbase status: {what}",
                          f"Coinbase's status page lists: {what}. The bot's prices and the "
                          f"stop checks come from Coinbase, so until it clears, stop raises "
                          f"and the stop watch may be working from stale or missing prices. "
                          f"Robinhood itself is not affected by this.",
                          tags="warning", click=base, quiet=True)
            if ok is not False:
                seen[key] = {"t": now_ts, "what": what, "loud": loud}
            log(f"  VENUE {venue} {'LOUD' if loud else 'quiet'}: {what}")
    if summary:
        log("  venues: " + ", ".join(summary))


def peg_prices(timeout=6):
    """{"USDC": price, "USDT": price} from Kraken's USD pairs; a coin the reply
    lacks is simply missing. Kraken keys the pairs USDCUSD and USDTZUSD
    (checked 2026-10-25): the first four letters name the coin, and "c" is
    the last trade as [price, size]."""
    data = fetch_json(PEG_URL, timeout)
    if data.get("error"):
        raise ValueError("; ".join(str(e) for e in data["error"]))
    out = {}
    for pair, tick in (data.get("result") or {}).items():
        coin = str(pair)[:4].upper()
        if coin not in PEG_COINS:
            continue
        try:
            px = float((tick.get("c") or [0])[0])
        except (TypeError, ValueError, AttributeError):
            continue
        if px > 0:
            out[coin] = px
    return out


def check_pegs(state, now_ts, prices=None):
    """Peg watch (2026-10-25): a loud push when USDC or USDT trades DEPEG_PCT
    or more off the dollar.

    Once per episode at max priority, again if the gap has widened by another
    DEPEG_PCT since the last push or after DEPEG_COOLDOWN_SEC, and a quiet
    note once the coin is back within REPEG_PCT. state["pegs"] holds the open
    episodes (since, sent, gap at the last push) and survives the daily
    reset. No prices, no push: the failure is logged.
    """
    if state is None:
        return
    try:
        prices = prices if prices is not None else peg_prices()
    except Exception as exc:
        log(f"  peg watch: no prices ({exc})")
        return
    pegs = state.setdefault("pegs", {})
    parts = []
    for coin in PEG_COINS:
        px = prices.get(coin)
        if not px:
            parts.append(f"{coin} ?")
            continue
        off = (px - 1.0) * 100.0            # signed; negative = under the dollar
        gap = abs(off)
        parts.append(f"{coin} {px:.4f}")
        ep = pegs.get(coin)
        if gap >= DEPEG_PCT:
            due = (not ep
                   or gap >= float(ep.get("gap") or 0) + DEPEG_PCT
                   or now_ts - float(ep.get("sent") or 0) >= DEPEG_COOLDOWN_SEC)
            if due:
                side = "under" if off < 0 else "over"
                body = [f"{coin} is ${px:.4f} on Kraken, {gap:.2f}% {side} $1.",
                        f"Information, not a trade: after a USDT move past {DEPEG_PCT:g}% from "
                        f"$1, bitcoin's odds of a sharp jump in the next five minutes are about "
                        f"five times normal (Perez Riaza & Gnabo 2025). The red lines are frozen "
                        f"until the Nov 5 review; the stops do the work.",
                        "Check: are your stops open and at their prices? A fast move can take "
                        "several out at once (the CRASH CHECK covers that).",
                        f"Repeats if the gap widens by another {DEPEG_PCT:g}% or after "
                        f"{DEPEG_COOLDOWN_SEC // 3600} hours; a quiet note when it is back "
                        f"within {REPEG_PCT:g}%."]
                if push(f"DEPEG: {coin} ${px:.4f}, {gap:.2f}% {side} $1", "\n".join(body),
                        priority="max", tags="rotating_light", loud=True) is not False:
                    pegs[coin] = {"since": (ep or {}).get("since") or now_ts,
                                  "sent": now_ts, "gap": gap}
            log(f"  PEG WATCH {coin} {px:.4f} ({gap:.2f}% off){'' if due else ' - said already'}")
        elif ep and gap <= REPEG_PCT:
            hours = (now_ts - float(ep.get("since") or now_ts)) / 3600.0
            push(f"{coin} back at peg: ${px:.4f}",
                 f"{coin} is back within {REPEG_PCT:g}% of $1, about {hours:.1f} hours after "
                 f"it first moved {DEPEG_PCT:g}% off.",
                 tags="white_check_mark", quiet=True)
            pegs.pop(coin, None)
            log(f"  PEG WATCH {coin} back at peg ({px:.4f})")
    log("  pegs: " + ", ".join(parts))


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
            EquityQuery("lt", ["intradayprice", MAX_PRICE]),
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
        if price < MIN_PRICE or price > MAX_PRICE:
            continue
        if vol < MIN_DAY_VOLUME:
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


def _coingecko(url):
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json",
                 "User-Agent": "market-movers-alerts/1.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def missing_robinhood(coins, allowed):
    """Robinhood symbols the top-N page did not include (stables aside)."""
    if not allowed:
        return []
    seen = {(c.get("symbol") or "").upper() for c in coins}
    return sorted(s for s in allowed
                  if s not in seen and s not in STABLE_SKIP and TICKER_RE.match(s))


def screen_crypto(allowed=None):
    """Top coins by market cap plus every other Robinhood coin, filtered to
    what is liquid and buyable.

    Returns None - NOT [] - when the screen could not be read at all, so the
    caller can tell "the universe is unknown this run" from "nothing
    qualified". See SCREEN_FAIL_ALERT_RUNS.
    """
    try:
        coins = _coingecko(COINGECKO_URL)
    except Exception as exc:
        log(f"  crypto screener failed: {exc}")
        return None

    if not isinstance(coins, list):
        log("  crypto screener returned an unexpected payload")
        return None

    if CRYPTO_FETCH_MISSING_RH:
        missing = missing_robinhood(coins, allowed)
        if missing:
            try:
                extra = _coingecko(COINGECKO_SYMBOLS_URL
                                   + ",".join(s.lower() for s in missing))
                if isinstance(extra, list):
                    seen = {(c.get("symbol") or "").upper() for c in coins}
                    added = [c for c in extra
                             if (c.get("symbol") or "").upper() not in seen]
                    coins = coins + added
                    log(f"  +{len(added)} Robinhood coins outside the top "
                        f"{CRYPTO_TOP_N}")
            except Exception as exc:
                # The top-N list still works; losing the tail is not fatal.
                log(f"  Robinhood tail fetch failed ({exc}) - top "
                    f"{CRYPTO_TOP_N} only this run")

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
            "cap": float(cap),
            "hour_pct": hour,
            # The week behind it (see CAUTION_WEEK_PCT: information only).
            "week_pct": c.get("price_change_percentage_7d_in_currency"),
            # Run maturity. The median crypto run is +10.9% end to end, so a
            # coin already well off its 24h low is late, not early.
            "low_24h": c.get("low_24h"),
            "candidate": float(chg) >= min(CRYPTO_ALERT_LEVELS),
        })
    if odd:
        log(f"  {odd} coins skipped for non-ticker symbols")
    if off:
        log(f"  {off} liquid coins skipped - not tradable on Robinhood")
    out.sort(key=lambda r: -r["pct"])
    return out


# --- State ------------------------------------------------------------------

def fresh_state(today, streak=None, alerts_today=None):
    return {"date": today, "fired": {}, "tradable": {}, "marks": {},
            "rh_symbols": None, "gate": GATE_VERSION,
            "cb_missing": {},          # coins Coinbase has no USD market for
            # name -> [send timestamps] for rate alerts. Carried across the
            # midnight-UTC reset (7pm Central) so "today" in the alert means
            # Kyle's day, not GitHub's; alert_count_line filters by local date.
            "alerts_today": alerts_today or {},
            "screen_fails": 0,         # consecutive failed crypto screens
            "streak": streak or {}}    # name -> {first, last, days}; survives the reset


def load_state():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        state = json.loads(STATE_FILE.read_text())
        if state.get("date") == today:
            state.setdefault("tradable", {})
            state.setdefault("marks", {})
            state.setdefault("rh_symbols", None)
            state.setdefault("cb_missing", {})
            state.setdefault("alerts_today", {})
            state.setdefault("screen_fails", 0)
            state.setdefault("streak", {})
            if state.get("gate") != GATE_VERSION:
                log("  tradability cache came from an older gate - clearing")
                state["tradable"] = {}
                state["gate"] = GATE_VERSION
            return state
        log("  state is from a previous day - starting fresh")
        # The multi-day memory is the one thing that must outlive the reset.
        fresh = fresh_state(today, prune_streak(state.get("streak") or {}),
                            prune_alert_log(state.get("alerts_today") or {}))
        fresh["pullback_sent"] = state.get("pullback_sent") or []
        # So an incident or a depeg that spans 7 PM Central is not announced
        # twice (2026-10-25).
        fresh["venue"] = state.get("venue") or {}
        fresh["pegs"] = state.get("pegs") or {}
        return fresh
    except (OSError, ValueError):
        log("  no previous state found")
    return fresh_state(today)


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except OSError as exc:
        log(f"  could not save state: {exc}")


# --- Multi-day memory --------------------------------------------------------
# Counted in Kyle's own calendar days, not UTC ones, so "day 3" means what he
# would mean by it. The rest of the state resets at midnight UTC (7pm CT);
# this map is carried across that reset by load_state.

def local_date(ts=None):
    """YYYY-MM-DD in USER_TZ; falls back to UTC if the zone is unavailable."""
    when = (datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None
            else datetime.now(timezone.utc))
    try:
        from zoneinfo import ZoneInfo
        when = when.astimezone(ZoneInfo(USER_TZ))
    except Exception:
        pass
    return when.strftime("%Y-%m-%d")


def _days_between(a, b):
    """Calendar days from date string a to date string b."""
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def prune_streak(streak, today=None):
    """Drop names that have gone quiet, so the memory never grows."""
    today = today or local_date()
    out = {}
    for key, s in (streak or {}).items():
        try:
            if _days_between(s["last"], today) <= STREAK_KEEP_DAYS:
                out[key] = s
        except (KeyError, TypeError, ValueError):
            continue
    return out


def note_streak(streak, key, today=None):
    """Record an alert for `key` today; return how many days running it has alerted.

    A gap of a day ends the streak. Called once per alert, but idempotent
    within a day, so repeats do not inflate the count.
    """
    today = today or local_date()
    s = streak.get(key)
    if s:
        try:
            gap = _days_between(s["last"], today)
        except (KeyError, TypeError, ValueError):
            gap = None
        if gap == 0:
            return int(s.get("days", 1))
        if gap == 1:
            s["last"] = today
            s["days"] = int(s.get("days", 1)) + 1
            return s["days"]
    streak[key] = {"first": today, "last": today, "days": 1}
    return 1


def streak_line(days, first):
    """The body line for a name that has alerted several days running."""
    if days < 2:
        return None
    try:
        weekday = datetime.strptime(first, "%Y-%m-%d").strftime("%a")
    except (TypeError, ValueError):
        weekday = first
    return f"still running: day {days} of alerts (first {weekday})"


def prune_alert_log(log_map, now_ts=None, keep_sec=36 * 3600):
    """Drop send timestamps older than a day and a half, and empty names."""
    now_ts = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    out = {}
    for key, times in (log_map or {}).items():
        kept = [t for t in (times or []) if isinstance(t, (int, float))
                and now_ts - t <= keep_sec]
        if kept:
            out[key] = kept
    return out


def alert_count_line(times, now_ts):
    """'1st alert today' or 'alert #3 today (first 9:37am)', from the send log.

    "Today" is the Central calendar day of `now_ts`; stamps from yesterday
    evening do not count even though they share a UTC date.
    """
    today = local_date(now_ts)
    times = [t for t in times if local_date(t) == today]
    n = len(times)
    if n <= 1:
        return "1st alert today"
    first = datetime.fromtimestamp(min(times), tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        first = first.astimezone(ZoneInfo(USER_TZ))
    except Exception:
        pass
    stamp = first.strftime("%I:%M%p").lstrip("0").lower()
    return f"alert #{n} today (first {stamp})"


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


def money(p):
    """Price string that survives both $85,000 and $0.0000061."""
    if p is None:
        return "?"
    return f"${p:,.2f}" if p >= 1 else f"${p:,.6f}".rstrip("0")


def cost_hints(price):
    """Break-even price and a stop that actually lands where you meant it.

    Buying pays ~EXEC_MARKUP_PCT over market and selling gives it up again, so
    break-even is the market price at which the sale nets the purchase back.
    The stop is quoted against Robinhood's bid, which already sits below the
    market, so a naive -10% triggers at roughly -9% of real price movement -
    the hint pushes it back out.
    """
    m = EXEC_MARKUP_PCT / 100.0
    breakeven = price * (1 + m) / (1 - m)
    stop = price * (1 - STOP_HINT_PCT / 100.0) * (1 - m)
    return breakeven, stop


# What a gold-star lot loses if its stop fills (2026-10-17; Kyle: "i don't
# like thinking i'm losing $50 when i'm losing $500"): the 15% stop from the
# hint, Robinhood's markup on the buy, and a fill about 1% under the stop
# (stop_check.FILL_SLIPPAGE_PCT; PYTH filled 0.96% under on Oct 17). A $500
# lot loses about $87, a $250 half lot about $44.
FILL_SLIPPAGE_PCT = 1.0


def stop_loss_usd(usd):
    """Dollars a new lot of `usd` loses against cost if its 15% stop fills."""
    m = EXEC_MARKUP_PCT / 100.0
    kept = ((1 - STOP_HINT_PCT / 100.0) * (1 - m) / (1 + m)
            * (1 - FILL_SLIPPAGE_PCT / 100.0))
    return float(usd) * (1 - kept)


def buy_head(note, usd):
    """The first line of a gold-star alert: the order, then what its stop
    costs if it fills, against what is paid."""
    head = f"BUY NOW: {note}, then the 15% stop below as soon as it fills"
    if usd:
        head += (f". If that stop fills: about -${stop_loss_usd(usd):,.0f} "
                 f"of the ${float(usd):,.0f}")
    # Robinhood's own price alert at the stop is the independent fill tell
    # (2026-10-18; strategy section 5, "Robinhood's own alerts").
    head += ". Then a Robinhood price alert at that stop."
    return head


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


def relvol_from_candles(candles, minutes, now_ts):
    """Window dollar volume against this coin's own pace, from 5-minute candles.

    `candles` are Coinbase rows [time, low, high, open, close, volume], any
    order. The window is the last `minutes`; "normal" is the average for a
    window that long over the COINBASE_BASELINE_H before it. Coinbase omits
    empty candles, so the baseline is measured by time span, not by count -
    a quiet coin's missing candles are zeros, not gaps. None when there is
    not enough history to have an opinion.
    """
    if not candles or not minutes or minutes <= 0:
        return None
    win_start = now_ts - minutes * 60.0
    base_start = win_start - COINBASE_BASELINE_H * 3600.0
    win = base = 0.0
    oldest = None
    for c in candles:
        try:
            t, close, vol = int(c[0]), float(c[4]), float(c[5])
        except (TypeError, ValueError, IndexError):
            continue
        dollars = close * vol
        if t >= win_start:
            win += dollars
        elif t >= base_start:
            base += dollars
            oldest = t if oldest is None else min(oldest, t)
    if oldest is None:
        return None
    base_minutes = (win_start - oldest) / 60.0 + 5.0
    if base_minutes < COINBASE_MIN_BASELINE_MIN:
        return None
    normal = base * (minutes / base_minutes)
    if normal <= 0:
        return None
    return win / normal


def coinbase_relvol(symbol, minutes, now_ts, missing=None):
    """Relative volume for the last `minutes` from Coinbase's own tape, or None.

    None means "no opinion" - Coinbase has no USD market for the coin, the
    call failed, or there is too little history - and the caller falls back
    to the CoinGecko figure. A 404 is remembered in `missing` for the day so
    a coin that is not on Coinbase costs one request, not one per run.
    """
    if not COINBASE_VOLUME_CONFIRM:
        return None
    sym = str(symbol).upper()
    if missing is not None and sym in missing:
        return None
    import urllib.error
    import urllib.request
    from urllib.parse import quote

    url = f"{COINBASE_API}/products/{quote(sym, safe='')}-USD/candles?granularity=300"
    req = urllib.request.Request(url, headers={"User-Agent": "market-movers-alerts/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=COINBASE_TIMEOUT) as resp:
            candles = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and missing is not None:
            missing[sym] = True
        return None
    except Exception:
        return None
    if not isinstance(candles, list):
        return None
    return relvol_from_candles(candles, minutes, now_ts)


def collect_rate(rows, marks, fired, now_ts, pct, cb_missing=None):
    """Names climbing steadily, on volume, enough to be worth a look.

    Compares against the OLDEST stamp still inside the window, so a delayed
    run widens the comparison rather than breaking it, and the elapsed
    minutes actually used are carried through to the alert text. Sorted by
    size of move, because that is the order the per-run cap should keep.

    For crypto the volume check comes from Coinbase's own candles once the
    price bar is cleared (see COINBASE_VOLUME_CONFIRM); the CoinGecko figure
    is the fallback. Which one answered is noted on the row as rv_source.

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
        rv = None
        if r["kind"] == "crypto":
            rv = coinbase_relvol(r["symbol"], mark[2], now_ts, cb_missing)
            r["rv_source"] = "coinbase" if rv is not None else "coingecko"
        if rv is None:
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

    CAVEAT, and it matters when reading the volume columns for tuning: these
    use relative_volume() - the CoinGecko 24h-derived figure for coins, the
    averageDailyVolume pace for stocks - NOT the Coinbase window figure the
    gate itself applies. Calling Coinbase for every tracked name every run
    would be ~90 requests a run, which is why the gate only asks for names
    already over the price bar. So "volume 5x:1" is not the same measurement
    as "on 5.1x volume" in an alert body, and the two should not be compared
    directly.
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


# --- Market-wide runs ---------------------------------------------------------
# The per-name screen is built for standouts, so a whole-market day (SPY up 4%
# in Oct 2026) never tripped it. These watch the two broad index funds direct
# and ping as each level is crossed, once per symbol per level per day.
INDEX_SYMBOLS = ("SPY", "QQQ")
INDEX_LEVELS = (1.0, 2.0)       # % above yesterday's close
INDEX_URGENT_LEVEL = 2.0        # this one breaks through Do Not Disturb


def index_moves(yf):
    """{symbol: % change from the prior close} for the index funds."""
    out = {}
    for sym in INDEX_SYMBOLS:
        try:
            fi = yf.Ticker(sym).fast_info
            last, prev = fi.last_price, fi.previous_close
            if last and prev:
                out[sym] = (last / prev - 1.0) * 100.0
        except Exception as exc:
            log(f"  index quote failed for {sym} ({exc})")
    return out


# Crypto breadth: the crypto version of a market-wide run. SPY/QQQ only see
# stocks, and the rate-based too_broad() needs most coins up 3% inside an hour
# on volume - it never saw the Oct 18 bounce, when 12 of 15 big coins rose
# 4-7% off their 24h lows over a Sunday afternoon. This looks at the largest
# tracked coins and asks how many are well off their 24h lows at once.
CBREADTH_TOP_N = 30             # largest coins by market cap, Robinhood-tradable
CBREADTH_OFF_LOW = 4.0          # a coin counts when it is this far off its 24h low
CBREADTH_SHARE = 0.60           # ...and this share of the group counts
CBREADTH_LEVELS = (4.0, 7.0)    # median off-low; the top level breaks DND
CBREADTH_HISTORY = ("history (Oct 2026 study): ~45% of 4% bounces reached +10% "
                    "off the low within 48h; ~1 in 3 fell back to the low")


def crypto_breadth(rows):
    """(count, total, median off-low %, leaders) for the largest coins."""
    big = sorted((r for r in rows if r.get("low_24h") and r.get("cap")),
                 key=lambda r: -r["cap"])[:CBREADTH_TOP_N]
    offs = []
    for r in big:
        try:
            offs.append(((r["price"] / float(r["low_24h"]) - 1) * 100, r["symbol"]))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    if not offs:
        return 0, 0, 0.0, []
    vals = sorted(o for o, _ in offs)
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    count = sum(1 for o in vals if o >= CBREADTH_OFF_LOW)
    leaders = sorted(offs, reverse=True)[:5]
    return count, n, median, leaders


def send_crypto_breadth(rows, state, now_ts, overnight=False):
    """Push each newly crossed crypto-breadth level, once per level per day.

    Overnight only the top level goes out (it breaks Do Not Disturb, like the
    other biggest-mover alerts). Returns the number sent.
    """
    count, total, median, leaders = crypto_breadth(rows)
    if total < 10 or count < CBREADTH_SHARE * total:
        return 0
    today = local_date(now_ts)
    fired = state.setdefault("cbreadth_fired", [])
    sent = 0
    for lvl in CBREADTH_LEVELS:
        key = f"{today}:{lvl:g}"
        if median < lvl or key in fired:
            continue
        urgent = lvl == max(CBREADTH_LEVELS)
        if overnight and not urgent:
            continue
        names = ", ".join(f"{sym} +{o:.1f}%" for o, sym in leaders)
        ok = push(f"CRYPTO-WIDE RUN - {count} of {total} coins up {CBREADTH_OFF_LOW:g}%+ off lows",
                  f"Median coin +{median:.1f}% off its 24h low (crossed +{lvl:g}%).\n"
                  f"Leaders off low: {names}\n{CBREADTH_HISTORY}",
                  priority="max" if urgent else "high", tags="rocket",
                  click="https://robinhood.com/crypto/BTC", mover=True)
        if ok is not False:
            fired.append(key)
            sent += 1
    return sent


def send_index_alerts(moves, state):
    """Push each newly crossed index level. Returns the number sent."""
    fired = state.setdefault("index_fired", [])
    sent = 0
    for sym, pct in sorted(moves.items(), key=lambda kv: -kv[1]):
        for lvl in INDEX_LEVELS:
            key = f"{sym}:{lvl}"
            if pct < lvl or key in fired:
                continue
            urgent = lvl >= INDEX_URGENT_LEVEL
            ok = push(f"{sym} +{pct:.1f}% - MARKET-WIDE RUN",
                      f"{sym} is up {pct:.1f}% on the day (crossed +{lvl:g}%). "
                      f"The whole market is moving, not one name.",
                      priority="max" if urgent else "high", tags="rocket",
                      click=f"https://robinhood.com/stocks/{sym}", mover=True)
            if ok is not False:
                fired.append(key)
                sent += 1
    return sent


def send_broad(kind, pending, rows, state, now_ts):
    """One heads-up when the whole board is climbing at once.

    too_broad() rightly holds the per-name rate alerts on a melt-up day - they
    would all be beta - but holding them silently meant a 4% market-wide day
    (Oct 2026) produced no alert at all. This sends a single summary instead,
    at most once per kind per day, led by the biggest names so it can still be
    acted on. Returns True if it sent.
    """
    if state is None:
        state = {}
    today = local_date(now_ts)
    sent = state.setdefault("broad_sent", {})
    if sent.get(kind) == today:
        return False
    top = sorted(pending, key=lambda t: -(t[1].get("pct") or 0))[:5]
    names = ", ".join(f"{t[1]['symbol']} {t[1].get('pct', 0):+.1f}%" for t in top)
    label = "STOCKS" if kind == "stock" else "CRYPTO"
    ok = push(f"MARKET-WIDE RALLY - {label}",
              f"{len(pending)} of {len(rows)} {kind}s climbing at once. "
              f"Leaders: {names}",
              priority="high", tags="rocket", mover=True)
    if ok is not False:
        sent[kind] = today
    return ok is not False


def alerts_so_far_today(state, key, now_ts):
    """How many rate alerts this name has already had today (Central day)."""
    if state is None:
        return 0
    today = local_date(now_ts)
    times = (state.get("alerts_today") or {}).get(key) or []
    return sum(1 for t in times if isinstance(t, (int, float)) and local_date(t) == today)


def tier_lines(r, rv, first):
    """TOP SETUP and the information lines, from the 60-day study (TOP_TIER_*).

    Returns (top, lines). The star needs a Coinbase-confirmed volume figure,
    because that is the tape the numbers were measured on.
    """
    lines, top = [], False
    week = r.get("week_pct")
    day = r.get("pct")
    if (first and r.get("kind") == "crypto" and r.get("rv_source") == "coinbase"
            and day is not None and day < TOP_TIER_MAX_DAY_PCT
            and rv is not None and rv >= TOP_TIER_MIN_RELVOL):
        top = True
        if TOP_SETUP_IS_BUY:
            lines.append(f"TOP SETUP - day still under {TOP_TIER_MAX_DAY_PCT:.0f}%, {rv:.0f}x volume: "
                         f"these averaged +11% under a 15% stop")
        else:
            lines.append(f"TOP SETUP (alert only since 10-16) - day under {TOP_TIER_MAX_DAY_PCT:.0f}%, "
                         f"{rv:.0f}x volume: +11% in the Aug-Sep study, -5% the month after "
                         f"(random hours +6%) - not a buy signal")
    cap = r.get("cap")
    if r.get("kind") == "crypto" and cap and cap < SMALL_CAP_USD:
        lines.append(f"small coin (${cap / 1e6:.0f}M cap): runs more often, "
                     f"falls just as hard - enter the 15% stop as soon as it fills")
    if week is not None and week >= CAUTION_WEEK_PCT:
        lines.append(f"hot week: up {week:.0f}% on the week (not a veto since 10-16: "
                     f"weekly momentum research)")
    if r.get("kind") == "crypto" and day is not None and day >= LATE_DAY_PCT:
        lines.append(f"LATE: already up {day:.0f}% today - coins past +{LATE_DAY_PCT:.0f}% "
                     f"hit -15% before +20% about 2 times in 3")
    return top, lines


def tag_lines(state, key, now_ts, count_today=True):
    """The '1st alert today' and 'still running: day N' lines for one name.

    Updates the send log and the multi-day memory as a side effect, so call
    it only when the alert is actually going out. With no state (tests, or
    an old caller) it returns nothing and changes nothing.
    """
    if state is None:
        return []
    out = []
    if count_today:
        times = state.setdefault("alerts_today", {}).setdefault(key, [])
        times.append(now_ts)
        out.append(alert_count_line(times, now_ts))
    streak = state.setdefault("streak", {})
    today = local_date(now_ts)
    days = note_streak(streak, key, today)
    line = streak_line(days, streak[key].get("first"))
    if line:
        out.append(line)
    return out


def context_lines(state, r, btc_price=None, now_ts=None):
    """The daily-checklist lines for one crypto alert (daily_context.py):
    BTC red lights, the volatility flag, the deep-dip test and Bitstamp depth
    for the stop's routing. Empty for stocks, without state, or without the
    module. Never raises - a data problem costs the lines, not the alert."""
    if dc is None or state is None or r.get("kind") != "crypto":
        return []
    out = []
    try:
        out.extend(dc.context_lines(state, r, btc_price, now_ts))
        # The Bitstamp line costs a request, so it is only looked up once the
        # daily picture exists (main() builds it before any alert is sent).
        line = (dc.thin_book_line(state, r["symbol"], now_ts, log=log)
                if state.get("daily") is not None else None)
        if line:
            out.append(line)
    except Exception as exc:
        log(f"  daily context lines failed for {r.get('symbol')} ({exc})")
    return out


def green_pass(state, now_ts):
    """Overnight: may a green signal (TOP SETUP, BASE BREAKOUT) go out?

    Runs start overnight (47% begin 9pm-6am CT) and these are the alerts the
    checklist says to act on, so they are not held until morning - but at
    most NIGHT_PASS_MAX per night, so a lively night cannot wreck his sleep.
    Without the module, nothing passes (the old behaviour).
    """
    if dc is None or state is None:
        return False
    try:
        return dc.night_pass(state, now_ts)
    except Exception:
        return False


def send_rate(pending, fired, now_ts, overnight, night_bar=OVERNIGHT_RATE_PCT,
              state=None, btc_price=None):
    held = 0
    for fkey, r, move, elapsed_min, rv in pending[:MAX_ALERTS_PER_RUN]:
        # First buzz or a repeat, and the star, are worked out BEFORE the
        # overnight decision because a TOP SETUP is allowed through it. Both
        # are pure reads; the send log is only written once the alert goes.
        key = f"{r['kind']}:{r['symbol']}"
        first = alerts_so_far_today(state, key, now_ts) == 0
        top, tiers = tier_lines(r, rv, first)

        # Overnight, only a genuine run is worth waking him for. Compared
        # against the RATE bar, not the day threshold - see OVERNIGHT_RATE_PCT
        # for what that mistake cost. Held signals are deliberately not
        # recorded, so a run still going at breakfast alerts again. A TOP
        # SETUP passes (see green_pass) - it is the one rate alert the
        # checklist calls a buy, and the move it reports usually began hours
        # before anyone is awake.
        if overnight and move < night_bar and not (
                top and TOP_SETUP_IS_BUY and green_pass(state, now_ts)):
            held += 1
            continue

        lines = [f"{r['name']}  -  {money(r['price'])}",
                 f"+{move:.1f}% in {elapsed_min:.0f} min on {rv:.0f}x volume"]

        day = f"day +{r['pct']:.1f}%"
        low = r.get("low_24h")
        if low and low > 0:
            off_low = (r["price"] / low - 1) * 100
            # Median run is ~10.9% end to end; past that he is buying maturity.
            day += f"  -  +{off_low:.0f}% off 24h low"
        lines.append(day)

        # The replay says first-vs-repeat matters more than the volume multiple.
        lines.extend(tag_lines(state, key, now_ts))
        lines.extend(tiers)
        lines.extend(context_lines(state, r, btc_price, now_ts))

        if r["kind"] == "crypto":
            breakeven, stop = cost_hints(r["price"])
            lines.append(f"break-even {money(breakeven)}  -  "
                         f"{STOP_HINT_PCT:.0f}% stop {money(stop)}")

        # A rate alert is a mover, not a buy: quiet feed, no star. TOP SETUP
        # is an alert only since 2026-10-16; with TOP_SETUP_IS_BUY it goes
        # through the same gold-star verdict as a BASE BREAKOUT (buy_verdict).
        title = f"{r['symbol']} +{move:.1f}% in {elapsed_min:.0f} min"
        tags, quiet, v = "zap", True, None
        if top and TOP_SETUP_IS_BUY:
            v = buy_verdict(r, f"+{move:.1f}% in {elapsed_min:.0f} min (TOP SETUP)",
                            now_ts, state, kind="top", lines=lines)
            title, tags, quiet = v["title"], v["tags"], v["quiet"]
            lines.insert(0, v["head"])
        if quiet:
            priority = QUIET_PRIORITY
        elif overnight:
            priority = "max"
        elif not first:
            priority = "default"
        else:
            priority = "high"
        ok = push(title,
                  "\n".join(lines),
                  priority=priority,
                  tags=tags,
                  click=robinhood_url(r),
                  quiet=quiet,
                  mover=quiet)        # a star is never a mover push
        if ok is not False:
            fired[fkey] = now_ts
            if v:
                note_star(state, r, v)
        src = r.get("rv_source")
        log(f"  RATE {r['symbol']} +{move:.1f}% over {elapsed_min:.0f}min "
            f"on {rv:.1f}x volume{' (' + src + ')' if src else ''} "
            f"(day {r['pct']:+.1f}%)"
            f"{' TOP' if top else ''}{'' if first else ' repeat'}"
            f"{' quiet' if quiet else ' LOUD, gold star'}")
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
        body = (f"{r['name']}\n"
                f"now +{r['pct']:.1f}%  -  ${r['price']:,.2f}\n"
                f"{ratio:.1f}x normal volume for this time of day")
        if push(f"{r['symbol']} unusual volume {ratio:.1f}x",
                body,
                priority=QUIET_PRIORITY,
                tags="eyes",
                click=robinhood_url(r),
                quiet=True, mover=True) is not False:
            fired[key] = now_ts
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


def send_alerts(pending, fired, high_level, overnight, state=None, btc_price=None,
                now_ts=None):
    sent, held = 0, 0
    for level, key, r in pending[:MAX_ALERTS_PER_RUN]:
        if overnight and level < high_level:
            # Held, deliberately not recorded in `fired`, so the morning runs
            # pick it up again if it is still running.
            held += 1
            continue
        priority = priority_for(level, high_level, overnight)
        body = format_body(r)
        # A threshold crossing counts toward the multi-day memory but not the
        # within-day tally - each level fires once a day by construction.
        # Use the run's clock: the daily-context cache is keyed by UTC day, so
        # re-reading it here can land on the other side of midnight from the
        # rest of the run and drop every checklist line.
        now_ts = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
        extra = tag_lines(state, f"{r['kind']}:{r['symbol']}", now_ts,
                          count_today=False)
        # A threshold alert is late-stage by definition; still say so when the
        # whole week is already in the price.
        extra += tier_lines(r, None, False)[1]
        extra += context_lines(state, r, btc_price, now_ts)
        if extra:
            body += "\n" + "\n".join(extra)
        # A crossing is a mover, never a buy under the rules: quiet feed.
        if push(f"{r['symbol']} crossed +{level:.0f}%",
                body,
                priority=QUIET_PRIORITY,
                click=robinhood_url(r),
                quiet=True, mover=True) is not False:
            fired[key] = level
            sent += 1
        log(f"  ALERT {r['symbol']} +{r['pct']:.1f}% "
            f"(crossed {level:.0f}%, {priority}, quiet feed)")

    if held:
        log(f"  {held} held until morning (below the overnight bar)")

    extra = len(pending) - MAX_ALERTS_PER_RUN
    if extra > 0 and sent:
        push("More movers crossing",
             f"{extra} more crossed a level. They'll follow shortly.",
             priority=QUIET_PRIORITY,
             tags="fire",
             quiet=True, mover=True)
        log(f"  {extra} queued for the next run")


# What the backtest said about trading these bounces (2026-10-18 study, 15
# coins, a year of hourly data): about 45% of +4% bounces reached +10% off the
# low within 48h, but buy-at-the-bounce / sell-at-+10% / stop-at-the-low lost
# about 2% a trade after Robinhood's spread in both halves of the year.
DIP_ODDS_LINE = "history: ~45% of bounces reach +10%; as a trade, -2% avg after spread"


def send_dip(coin, st, price):
    breakeven, stop = cost_hints(price)
    row = {"kind": "crypto", "symbol": coin["symbol"]}
    body = "\n".join([
        f"{coin['symbol']}  -  {money(price)}",
        f"{st['off_high']:.1f}% below 7-day high {money(st['high'])}",
        f"+{st['off_low']:.1f}% off 12h low {money(st['low'])} - turning up",
        f"break-even {money(breakeven)}  -  "
        f"{STOP_HINT_PCT:.0f}% stop {money(stop)}",
        f"watching because: {coin['why']}" if coin.get("why") else "",
        DIP_ODDS_LINE,
    ]).strip()
    push(f"{coin['symbol']} dip turning up ({st['off_high']:.0f}% off high)",
         body, priority="default", tags="arrow_heading_up",
         click=robinhood_url(row), mover=True)
    log(f"  DIP {coin['symbol']} {st['off_high']:.1f}% off 7d high, "
        f"+{st['off_low']:.1f}% off 12h low")


def send_breakouts(pending, fired, now_ts, overnight, state=None, btc_price=None):
    """BASE BREAKOUT alerts - the checklist's other green light.

    `pending` comes from daily_context.collect_breakouts: coins whose prior
    14 daily closes sat in a 15% range, up 5%+ today, above the 50-day
    average, on a weekday. One alert per coin per UTC day. Red lines from the
    checklist (BTC, hot week, LATE, deep dip) travel with it, and when any is
    present it is not a buy: the setup is right but the day is not. The gold
    star and the loud feed are for a breakout the rules allow right now
    (buy_verdict).
    """
    if dc is None:
        return
    held = 0
    for key, r, ctx in pending[:MAX_ALERTS_PER_RUN]:
        if overnight and not green_pass(state, now_ts):
            held += 1
            continue

        lines = [f"{r['name']}  -  {money(r['price'])}"]
        lines.extend(dc.breakout_lines(r, ctx))
        k = f"{r['kind']}:{r['symbol']}"
        lines.extend(tag_lines(state, k, now_ts))
        lines.extend(tier_lines(r, None, False)[1])     # small coin, hot week, LATE
        lines.extend(context_lines(state, r, btc_price, now_ts))
        breakeven, stop = cost_hints(r["price"])
        lines.append(f"break-even {money(breakeven)}  -  "
                     f"{STOP_HINT_PCT:.0f}% stop {money(stop)}")

        red = first_red(lines)
        v = buy_verdict(r, f"base breakout +{ctx['day_pct']:.1f}% today", now_ts,
                        state, kind="base", lines=lines)
        lines.insert(0, v["head"])
        quiet = v["quiet"]
        priority = QUIET_PRIORITY if quiet else STAR_PRIORITY
        ok = push(v["title"], "\n".join(lines),
                  priority=priority,
                  tags=v["tags"],
                  click=robinhood_url(r),
                  quiet=quiet)
        if ok is not False:
            fired[key] = dc.utc_day(now_ts)
            note_star(state, r, v, now_ts, f"base breakout +{ctx['day_pct']:.1f}% today")
        log(f"  BREAKOUT {r['symbol']} +{ctx['day_pct']:.1f}% today from a "
            f"{ctx['base14']:.0f}% base, {ctx['vs50_pct']:+.0f}% vs 50-day"
            f"{' (red lines)' if red else ''}{' quiet' if quiet else ' LOUD, gold star'}")
    if held:
        log(f"  {held} base breakouts held - overnight cap reached")


def run_breakouts(crypto_rows, fired, now_ts, overnight, state, tradable, btc_price):
    """The breakout check end to end. Never fatal to the main alerts."""
    if dc is None:
        return
    try:
        raw = dc.collect_breakouts(state, crypto_rows, fired, now_ts)
        pending = filter_tradable(raw, tradable, 1)
        if raw:
            dropped = len(raw) - len(pending)
            log(f"{len(pending)} base breakouts"
                + (f" ({dropped} dropped - not on Robinhood)" if dropped else ""))
        send_breakouts(pending, fired, now_ts, overnight, state, btc_price)
    except Exception as exc:
        log(f"  base breakout check failed ({exc}) - main alerts unaffected")


def crypto_screen_failed(state):
    """Record a run where the coin universe could not be read.

    Skipping the crypto stages matters less than saying so: an empty universe
    cannot produce an alert anyway, but it CAN produce a run that looks quiet
    and healthy. Pushes once, when the streak reaches SCREEN_FAIL_ALERT_RUNS,
    not on every run of a long outage.
    """
    n = int(state.get("screen_fails") or 0) + 1
    state["screen_fails"] = n
    log(f"  the coin universe is unknown this run, not empty - crypto stages "
        f"skipped ({n} run{'' if n == 1 else 's'} in a row)")
    if n == SCREEN_FAIL_ALERT_RUNS:
        push("Crypto screen down",
             f"The coin screen has failed {n} runs in a row (about "
             f"{n * 5} minutes). No crypto alert can fire until it recovers. "
             f"Usually CoinGecko rate-limiting, which normally clears itself; "
             f"if it does not, check the run log.",
             priority="high", tags="warning", loud=True)
    return n


def crypto_screen_recovered(state):
    """Clear the failure streak, and say so if there was one worth noting."""
    n = int(state.get("screen_fails") or 0)
    if n:
        log(f"  coin screen recovered after {n} failed "
            f"run{'' if n == 1 else 's'}")
    state["screen_fails"] = 0


def refresh_daily_context(state, crypto_rows, now_ts):
    """Top up the once-a-day picture of every coin. Never fatal."""
    if dc is None:
        log("  daily_context.py not present - checklist lines and breakouts off")
        return
    try:
        dc.refresh(state, crypto_rows, now_ts, log=log)
        daily = state.get("daily") or {}
        today = dc.utc_day(now_ts)
        fresh = [s for s, e in daily.items()
                 if not s.startswith("_") and e.get("d") == today and not e.get("short")]
        coiled = []
        for r in crypto_rows:
            ctx = dc.coin_context(daily.get(r["symbol"]), r["price"], today)
            if (ctx and ctx.get("base14") is not None
                    and ctx["base14"] <= dc.BASE_RANGE_MAX_PCT
                    and (ctx.get("vs50_pct") or 0) > 0):
                coiled.append(r["symbol"])
        log(f"  daily context for {len(fresh)} of {len(crypto_rows)} coins; "
            f"{len(coiled)} on a tight base above the 50-day"
            + (f": {', '.join(sorted(coiled)[:12])}" if coiled else ""))
    except Exception as exc:
        log(f"  daily context refresh failed ({exc}) - alerts unaffected")


def run_watchlist(crypto_rows, marks, fired, now_ts, overnight, state=None,
                  btc_price=None):
    """Closer scrutiny for the coins in watchlist.json. Never fatal."""
    try:
        import watchlist as wl
        coins = wl.load_watchlist()
        if not coins:
            return
        rows = wl.watch_rows(crypto_rows, coins)
        missing = {c["symbol"] for c in coins} - {r["symbol"] for r in rows}
        if missing:
            log(f"  watchlist: not in the tradable universe: "
                f"{', '.join(sorted(missing))}")
        # Early climb at the lower bar. Same cooldown key as the ordinary
        # rate signal, so a coin the 3% bar already sent is skipped here.
        cb_missing = state.get("cb_missing") if state else None
        early, _, _ = collect_rate(rows, marks, fired, now_ts,
                                   wl.WATCH_RATE_PCT, cb_missing)
        if early:
            log(f"  watchlist: {len(early)} early climbs at "
                f"{wl.WATCH_RATE_PCT:g}%")
        send_rate(early, fired, now_ts, overnight, state=state, btc_price=btc_price)
        wl.check_dips(coins, {r["symbol"]: r["price"] for r in rows},
                      fired, now_ts, overnight, send_dip, log=log)
    except Exception as exc:
        log(f"  watchlist failed ({exc}) - main alerts unaffected")


# ---------------------------------------------------------------------------

def main():
    # The test push comes first so it works at any hour, market open or not.
    if os.environ.get("TEST_PUSH") == "1":
        ok = push("Market Movers test",
                  "Phone alerts are wired up correctly.\nTap to open Robinhood.\n"
                  "A buy comes with a gold star in front of the title; "
                  "nothing else has one.",
                  priority="default", tags="white_check_mark",
                  click="https://robinhood.com/stocks/", loud=True)
        quiet_ok = push("Market Movers test - quiet feed",
                        "This is the quiet feed: alerts that are not a buy "
                        "under the rules. It does not need to buzz.",
                        tags="white_check_mark", quiet=True)
        log("Test notifications sent" if ok and quiet_ok else "Test notification FAILED")
        return 0 if ok and quiet_ok else 1

    force = os.environ.get("FORCE_RUN") == "1"
    overnight = in_overnight()
    now_ts = datetime.now(timezone.utc).timestamp()
    state = load_state()
    # Gold stars sent this run count against the book (buy_decision). Reset
    # every run and dropped before the save: a star is not a fill.
    state["run_stars"] = []
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
    screen_down = crypto_rows is None
    if screen_down:
        crypto_rows = []
        crypto_screen_failed(state)
    else:
        crypto_screen_recovered(state)
    crypto_movers = [r for r in crypto_rows if r.get("candidate")]
    if not screen_down:
        log(f"tracking {len(crypto_rows)} liquid coins, "
            f"{len(crypto_movers)} in alert range")
    record_marks(crypto_rows, marks, now_ts)

    # The daily picture behind the checklist lines and the breakout alert.
    # BTC's live price anchors the "BTC down N%" lines.
    btc_price = next((r["price"] for r in crypto_rows if r["symbol"] == "BTC"), None)
    if not screen_down:
        refresh_daily_context(state, crypto_rows, now_ts)

    # Rate of change first: it is the earliest signal, so it wins the run cap.
    if crypto_rows:
        send_crypto_breadth(crypto_rows, state, now_ts, overnight)
    craw, cquiet, cunknown = collect_rate(crypto_rows, marks, fired, now_ts,
                                          CRYPTO_RATE_PCT, state["cb_missing"])
    if cquiet or cunknown:
        log(f"  {cquiet} coins climbing on ordinary volume"
            + (f", {cunknown} with no volume figure" if cunknown else ""))
    # Which tape answered the volume question. If the fallback count ever
    # dominates, Coinbase is unreachable from the runner and the gate has
    # quietly gone back to the slow global figure.
    on_cb = sum(1 for r in crypto_rows if r.get("rv_source") == "coinbase")
    on_cg = sum(1 for r in crypto_rows if r.get("rv_source") == "coingecko")
    if on_cb or on_cg:
        log(f"  volume checked on Coinbase for {on_cb} coins over the price bar"
            + (f", CoinGecko fallback for {on_cg}" if on_cg else ""))
    if too_broad(craw, crypto_rows):
        log(f"  {len(craw)} of {len(crypto_rows)} coins climbing - "
            f"market-wide, holding rate alerts")
        send_broad("coin", craw, crypto_rows, state, now_ts)
        craw = []
    crate = filter_tradable(craw, tradable, 1)
    if craw:
        dropped = len(craw) - len(crate)
        log(f"{len(crate)} coins climbing fast enough to flag"
            + (f" ({dropped} dropped - not on Robinhood)" if dropped else ""))
    send_rate(crate, fired, now_ts, overnight, state=state, btc_price=btc_price)
    if not screen_down:
        log(rate_bars(crypto_rows, marks, now_ts))
    if not screen_down:
        run_watchlist(crypto_rows, marks, fired, now_ts, overnight, state,
                      btc_price)
    # Tight-base breakouts: the checklist's second green light, independent
    # of the rate signal's volume gate.
    if not screen_down:
        run_breakouts(crypto_rows, fired, now_ts, overnight, state, tradable,
                      btc_price)
    # Rule 1: buy signals that came in outside 7 AM - 7 PM, re-checked once at
    # the first run inside the window; a gold star for each one still a buy.
    try:
        send_night_digest(state, crypto_rows, now_ts, btc_price)
    except Exception as exc:
        log(f"  overnight digest failed ({exc}) - alerts unaffected")
    # A gold star the phone may have missed: STILL A BUY at 10 and 30 minutes
    # while the coin is still a buy and not in the book (2026-10-21).
    try:
        if not screen_down:
            send_star_repeats(state, crypto_rows, now_ts, btc_price)
    except Exception as exc:
        log(f"  star repeat check failed ({exc}) - alerts unaffected")
    # Held coins under a stop's sell limit, or several at their stops.
    try:
        check_live_stops(state, now_ts)
    except Exception as exc:
        log(f"  stop watch failed ({exc}) - alerts unaffected")
    # Venue status (Robinhood loud while lots are held, Coinbase quiet) and
    # the stablecoin pegs (2026-10-25).
    try:
        check_venues(state, now_ts)
    except Exception as exc:
        log(f"  venue watch failed ({exc}) - alerts unaffected")
    try:
        check_pegs(state, now_ts)
    except Exception as exc:
        log(f"  peg watch failed ({exc}) - alerts unaffected")

    crypto_pending = filter_tradable(
        collect_pending(crypto_movers, CRYPTO_ALERT_LEVELS, fired, "crypto"),
        tradable, 2)
    if not screen_down:
        log(f"{len(crypto_pending)} new tradable crypto threshold crossings")
    send_alerts(crypto_pending, fired, CRYPTO_HIGH_PRIORITY_LEVEL, overnight,
                state=state, btc_price=btc_price, now_ts=now_ts)

    # --- Pullback-in-uptrend (SPY/QQQ), once the daily bar is final ---
    now_et = local_now(MARKET_TZ)
    if pullback.in_window(now_et) or force:
        try:
            import yfinance as yf
            today_et = (now_et or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
            closes, dates = {}, {}
            for sym in pullback.PULLBACK_SYMBOLS:
                closes[sym], dates[sym] = pullback.fetch_daily(yf, sym, today_et)
            pullback.check_pullbacks(closes, dates, state, push, log)
        except Exception as exc:
            log(f"  pullback check failed ({exc}) - alerts unaffected")

    # --- Stocks: market hours only ---
    if force or market_is_open():
        try:
            import yfinance as yf
        except ImportError:
            log("yfinance is not installed")
            state.pop("run_stars", None)
            save_state(state)
            return 2

        try:
            moves = index_moves(yf)
            if moves:
                log("  index: " + ", ".join(f"{k} {v:+.2f}%" for k, v in moves.items()))
                send_index_alerts(moves, state)
        except Exception as exc:
            log(f"  index watch failed ({exc}) - alerts unaffected")

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
            send_broad("stock", sraw, stock_rows, state, now_ts)
            sraw = []
        srate = filter_tradable(sraw, tradable, 1)
        if sraw:
            dropped = len(sraw) - len(srate)
            log(f"{len(srate)} stocks climbing fast enough to flag"
                + (f" ({dropped} dropped - not on Robinhood)" if dropped else ""))
        send_rate(srate, fired, now_ts, overnight, state=state)
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
        send_alerts(stock_pending, fired, HIGH_PRIORITY_LEVEL, overnight,
                    state=state, now_ts=now_ts)
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

    state.pop("run_stars", None)
    save_state(state)
    if PUSH_STATS["failed"]:
        log(f"{PUSH_STATS['failed']} notification(s) failed after two tries - "
            f"exiting with an error so this run shows as failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
