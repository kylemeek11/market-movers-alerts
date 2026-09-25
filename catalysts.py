#!/usr/bin/env python3
"""
Catalyst watcher — what could move next, as opposed to what is moving now.

Companion to alerts_bot.py, not a replacement. That one answers "something is
running." This one answers "a binary event lands in N days, and here is who is
levered to it."

Three jobs:

  --weekly   Digest of binary events falling inside the window, from the
             hand-mapped read-through list AND from a discovery search of
             ClinicalTrials.gov, plus the squeeze screen on watchlist names.

  --daily    Poll for CHANGES: a new EDGAR filing on a watched company, a
             trial's status flipping, or a primary completion date moving.
             A date moving is itself a signal - it is the sponsor telling you
             the readout slipped or accelerated.

  --test     Push a test notification and exit.

WHAT THIS IS NOT. A catalyst calendar tells you WHEN volatility lands, not
which direction. BRUNELLO could have missed on 2026-09-24 and SRZN would
plausibly have halved on the same morning for the same reason. Phase 3 is
close to a coin flip. Treat every line this emits as "a binary event is
coming", never as "this will go up."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("CATALYST_CONFIG", HERE / "catalysts.json"))
STATE_FILE = Path(os.environ.get("CATALYST_STATE", "state/catalysts.json"))

NTFY_TOPIC = os.environ.get("NTFY_CATALYST_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

# The SEC asks for a contact address in the User-Agent and will throttle or
# block requests without one. This is not optional politeness; it is their
# stated access policy.
SEC_UA = os.environ.get("SEC_USER_AGENT", "MarketMovers catalyst watcher")

CTGOV = "https://clinicaltrials.gov/api/v2/studies"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"

FIELDS = "|".join([
    "NCTId", "BriefTitle", "OverallStatus", "Phase", "LeadSponsorName",
    "PrimaryCompletionDate", "PrimaryCompletionDateType", "EnrollmentCount",
    "Condition",
])


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# --- plumbing ---------------------------------------------------------------

def get_json(url, headers=None, tries=3):
    """GET with retries. Returns None rather than raising: a dead API should
    make the run quiet, not crash it into a red X every five minutes."""
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:
            if attempt == tries - 1:
                log(f"  fetch failed: {url.split('?')[0]} — {exc}")
                return None
            time.sleep(2 * (attempt + 1))
    return None


def push(title, message, priority="default", tags="calendar", click=None):
    if not NTFY_TOPIC:
        log("NTFY_CATALYST_TOPIC is not set — printing instead of sending")
        log(f"  {title}\n{message}")
        return

    def ascii_only(s):
        return "".join(ch if 32 <= ord(ch) < 127 else "-" for ch in str(s))

    headers = {"Title": ascii_only(title)[:200], "Priority": priority,
               "Tags": tags}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                                 data=str(message).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 300:
                log(f"  ntfy returned {resp.status}")
    except Exception as exc:
        log(f"  push failed: {exc}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        log("  no previous state — first run")
        return {"trials": {}, "filings": {}, "seen_discovery": []}


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))
    except OSError as exc:
        log(f"  could not save state: {exc}")


def load_config(path=None):
    return json.loads(Path(path or CONFIG_FILE).read_text())


# --- ClinicalTrials.gov -----------------------------------------------------

def flatten_study(study):
    """The v2 payload nests everything. Pull out the fields that matter."""
    p = study.get("protocolSection", {})
    ident = p.get("identificationModule", {})
    status = p.get("statusModule", {})
    design = p.get("designModule", {})
    pcd = status.get("primaryCompletionDateStruct", {}) or {}
    return {
        "nct": ident.get("nctId"),
        "title": ident.get("briefTitle", ""),
        "sponsor": (p.get("sponsorCollaboratorsModule", {})
                     .get("leadSponsor", {}).get("name", "")),
        "status": status.get("overallStatus", ""),
        "phase": "/".join(design.get("phases", []) or []),
        "pcd": pcd.get("date"),
        "pcd_type": pcd.get("type"),
        "enrollment": (design.get("enrollmentInfo", {}) or {}).get("count"),
        "conditions": p.get("conditionsModule", {}).get("conditions", []),
    }


def fetch_trials(nct_ids, fetch=get_json):
    """Look up specific trials by ID. Chunked: the filter has a length limit."""
    out = {}
    ids = list(nct_ids)
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        q = urllib.parse.urlencode({"filter.ids": ",".join(chunk),
                                    "fields": FIELDS, "pageSize": len(chunk)})
        data = fetch(f"{CTGOV}?{q}")
        for s in (data or {}).get("studies", []):
            row = flatten_study(s)
            if row["nct"]:
                out[row["nct"]] = row
    return out


def discover_trials(cluster, window_days, fetch=get_json):
    """Find catalysts nobody told us about.

    Keyed on condition and phase rather than drug or sponsor name, because
    names are unreliable: BRUNELLO is filed under 'EyeBiotech Ltd.' with the
    intervention called 'EYE103'. Searching for Merck, MK-3000 or remigromig
    would all have returned nothing.
    """
    disc = cluster.get("discovery") or {}
    conds = disc.get("conditions") or []
    if not conds:
        return []
    phases = disc.get("phases") or ["PHASE2", "PHASE3"]
    today = date.today()
    end = today + timedelta(days=window_days)

    advanced = (f"AREA[PrimaryCompletionDate]RANGE[{today.isoformat()},"
                f"{end.isoformat()}] AND "
                f"AREA[Phase]({' OR '.join(phases)})")
    q = urllib.parse.urlencode({
        "query.cond": " OR ".join(conds),
        "filter.advanced": advanced,
        "filter.overallStatus":
            "ACTIVE_NOT_RECRUITING|RECRUITING|ENROLLING_BY_INVITATION",
        "fields": FIELDS,
        "pageSize": "100",
        "countTotal": "true",
    })
    data = fetch(f"{CTGOV}?{q}")
    rows = [flatten_study(s) for s in (data or {}).get("studies", [])]

    floor = disc.get("min_enrollment") or 0
    return [r for r in rows
            if r["nct"] and (r["enrollment"] or 0) >= floor]


# --- EDGAR ------------------------------------------------------------------

def fetch_filings(cik, fetch=get_json, limit=15):
    """Recent filings for one company. cik is the zero-padded 10-digit form."""
    data = fetch(EDGAR_SUBMISSIONS.format(cik=str(cik).zfill(10)),
                 headers={"User-Agent": SEC_UA,
                          "Accept-Encoding": "gzip, deflate"})
    recent = ((data or {}).get("filings", {}) or {}).get("recent", {}) or {}
    forms = recent.get("form") or []
    out = []
    for i in range(min(limit, len(forms))):
        out.append({
            "form": forms[i],
            "date": recent["filingDate"][i],
            "items": (recent.get("items") or [""] * len(forms))[i],
            "accession": recent["accessionNumber"][i],
            "doc": (recent.get("primaryDocument") or [""] * len(forms))[i],
        })
    return out


def filing_url(cik, accession, doc):
    acc = accession.replace("-", "")
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
            if doc else
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/")


# --- the squeeze screen -----------------------------------------------------

def squeeze_profile(ticker):
    """Float and short interest. A binary event on a small, heavily shorted
    float is what turns a justified re-rating into a violent one - SRZN had
    4.62M float with 31.3% of it short and traded more than its entire float
    on the morning of the readout. Returns None when the data is unavailable,
    which is common for small caps and is not an error."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        info = yf.Ticker(ticker).get_info()
    except Exception:
        return None
    if not info:
        return None
    flt = info.get("floatShares")
    short = info.get("sharesShort")
    pct = info.get("shortPercentOfFloat")
    if pct is not None and pct < 1:          # yfinance returns a fraction
        pct *= 100
    if pct is None and flt and short:
        pct = short / flt * 100
    return {"float": flt, "short": short, "short_pct": pct,
            "price": info.get("currentPrice") or info.get("regularMarketPrice")}


def squeeze_flag(prof, settings):
    if not prof or not prof.get("float") or prof.get("short_pct") is None:
        return False
    return (prof["float"] <= settings.get("squeeze_float_max", 15_000_000)
            and prof["short_pct"] >= settings.get("squeeze_short_pct_min", 15.0))


# --- formatting -------------------------------------------------------------

def days_until(iso):
    try:
        return (date.fromisoformat(iso) - date.today()).days
    except (TypeError, ValueError):
        return None


def ctgov_url(nct):
    return f"https://clinicaltrials.gov/study/{nct}"


def describe(trial, links):
    """One line per catalyst. `links` are the (ticker, direction) pairs that
    care about this trial."""
    d = days_until(trial["pcd"])
    when = f"{trial['pcd']}" + (f" ({d}d)" if d is not None else "")
    who = ", ".join(f"{t}{'' if dirn == 'own' else ' ' + dirn}"
                    for t, dirn in links) or trial["sponsor"][:22]
    est = "~" if (trial.get("pcd_type") or "").upper() == "ESTIMATED" else ""
    n = f" n={trial['enrollment']}" if trial.get("enrollment") else ""
    return f"{est}{when}  {who}  {trial['phase']}{n}"


# --- jobs -------------------------------------------------------------------

def watched_map(config):
    """nct -> [(ticker, direction), ...] and ticker -> entry."""
    by_nct, by_ticker = {}, {}
    for cluster in config.get("clusters", []):
        for name in cluster.get("names", []):
            by_ticker[name["ticker"]] = name
            for link in name.get("levered_to", []):
                by_nct.setdefault(link["nct"], []).append(
                    (name["ticker"], link.get("direction", "own")))
    return by_nct, by_ticker


def run_weekly(config, state, fetch=get_json):
    settings = config.get("settings", {})
    window = settings.get("weekly_window_days", 45)
    by_nct, by_ticker = watched_map(config)

    tracked = fetch_trials(by_nct.keys(), fetch=fetch)
    log(f"{len(tracked)} mapped trials resolved")

    due = []
    for nct, trial in tracked.items():
        d = days_until(trial["pcd"])
        if d is not None and -14 <= d <= window:
            due.append((d, trial, by_nct.get(nct, [])))

    found = []
    for cluster in config.get("clusters", []):
        for trial in discover_trials(cluster, window, fetch=fetch):
            if trial["nct"] in tracked:
                continue
            # Do not trust the server-side date filter to stay correct. A
            # query that silently stops filtering would turn this digest into
            # a dump of every retina trial on the registry.
            d = days_until(trial["pcd"])
            if d is None or not (-14 <= d <= window):
                continue
            found.append((d, trial, []))
    log(f"{len(due)} mapped catalysts due, {len(found)} unmapped found")

    if not due and not found:
        log("nothing due — staying quiet")
        return 0

    # The squeeze profile is only worth reporting NEXT TO a catalyst. A small,
    # heavily shorted float with no event coming is not news, and repeating it
    # every Sunday is how a weekly alert becomes wallpaper. So it is looked up
    # only for names that have something due, and printed under that event.
    due_tickers = {t for _, _, links in due for t, _ in links}
    profiles = {}
    for ticker in due_tickers:
        prof = squeeze_profile(ticker)
        if squeeze_flag(prof, settings):
            profiles[ticker] = prof

    lines = []
    if due:
        lines.append("ON THE MAP")
        for _, trial, links in sorted(due):
            lines.append("  " + describe(trial, links))
            for ticker, _dirn in links:
                p = profiles.get(ticker)
                if p:
                    lines.append(
                        f"    {ticker}: float {p['float']/1e6:.1f}M, "
                        f"{p['short_pct']:.0f}% short — moves hard either way")
    if found:
        lines.append("")
        lines.append("UNMAPPED (sponsor shown; map it or ignore it)")
        for _, trial, _ in sorted(found)[:12]:
            lines.append("  " + describe(trial, []))

    lines.append("")
    lines.append("Timing, not direction. A miss moves these just as hard.")
    push(f"Catalysts: next {window} days", "\n".join(lines),
         priority="default", tags="calendar")
    log("weekly digest sent")
    return 0


def run_daily(config, state, fetch=get_json):
    settings = config.get("settings", {})
    by_nct, by_ticker = watched_map(config)
    sent = 0

    # 1. Trials whose status or date moved since we last looked. A primary
    #    completion date sliding is the sponsor telling you something.
    tracked = fetch_trials(by_nct.keys(), fetch=fetch)
    for nct, trial in tracked.items():
        prev = state["trials"].get(nct) or {}
        changes = []
        if prev.get("status") and prev["status"] != trial["status"]:
            changes.append(f"status {prev['status']} -> {trial['status']}")
        if prev.get("pcd") and prev["pcd"] != trial["pcd"]:
            changes.append(f"completion {prev['pcd']} -> {trial['pcd']}")
        if changes:
            who = ", ".join(t for t, _ in by_nct.get(nct, []))
            push(f"{nct} changed",
                 f"{who}\n" + "\n".join(changes) + f"\n{trial['title'][:90]}",
                 priority="high", tags="warning", click=ctgov_url(nct))
            log(f"  TRIAL {nct}: {'; '.join(changes)}")
            sent += 1
        state["trials"][nct] = {"status": trial["status"], "pcd": trial["pcd"]}

    # 2. New filings on watched companies.
    watched_forms = set(settings.get("edgar_forms_watched", ["8-K"]))
    urgent_items = set(settings.get("edgar_urgent_items", []))
    for ticker, entry in by_ticker.items():
        cik = entry.get("cik")
        if not cik:
            continue
        seen = set(state["filings"].get(ticker, []))
        filings = fetch_filings(cik, fetch=fetch)
        fresh = [f for f in filings
                 if f["form"] in watched_forms and f["accession"] not in seen]
        # First sight of a company: record without alerting, or the first run
        # would fire a notification for every historical filing.
        if not seen:
            state["filings"][ticker] = [f["accession"] for f in filings][:40]
            log(f"  {ticker}: baseline recorded ({len(filings)} filings)")
            continue
        for f in fresh:
            items = [i.strip() for i in (f["items"] or "").split(",") if i.strip()]
            hot = bool(urgent_items & set(items))
            body = (f"{f['form']}"
                    + (f" item {', '.join(items)}" if items else "")
                    + f"\nfiled {f['date']}\n{entry.get('note', '')[:110]}")
            push(f"{ticker}: new {f['form']}", body,
                 priority="high" if hot else "default", tags="page_facing_up",
                 click=filing_url(cik, f["accession"], f["doc"]))
            log(f"  FILING {ticker} {f['form']} {f['date']}")
            sent += 1
        state["filings"][ticker] = (
            [f["accession"] for f in filings] + list(seen))[:40]

    log(f"{sent} alerts sent")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    if args.test:
        push("Catalyst watcher test",
             "Wired up. You'll get a weekly calendar and same-day filing "
             "alerts on the mapped names.",
             tags="white_check_mark")
        log("test notification sent")
        return 0

    config = load_config(args.config)
    state = load_state()
    try:
        if args.weekly:
            rc = run_weekly(config, state)
        elif args.daily:
            rc = run_daily(config, state)
        else:
            ap.error("pick --weekly, --daily or --test")
            return 2
    finally:
        save_state(state)
    return rc


if __name__ == "__main__":
    sys.exit(main())
