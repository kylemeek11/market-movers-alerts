#!/usr/bin/env python3
"""Replays the SRZN / BRUNELLO case against the real API payload shapes.

The question this answers: standing on 1 September 2026, would the watcher
have told him a binary event was landing on Surrozen? Fixtures are trimmed
copies of genuine responses pulled from clinicaltrials.gov and data.sec.gov
on 2026-09-24, so the parsers are tested against the real shapes rather than
against what the docs imply.
"""

import datetime
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("cat", Path(__file__).parent / "catalysts.py")
cat = importlib.util.module_from_spec(spec)
sys.modules["cat"] = cat
spec.loader.exec_module(cat)

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


# --- fixtures ---------------------------------------------------------------

BRUNELLO = {
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT06571045",
            "briefTitle": "A Study to Evaluate the Efficacy and Safety of 2 Doses of EYE103 Compared With Ranibizumab (0.5 mg) in Participants With DME",
        },
        "statusModule": {
            "overallStatus": "ACTIVE_NOT_RECRUITING",
            "primaryCompletionDateStruct": {"date": "2026-09-30", "type": "ESTIMATED"},
            "completionDateStruct": {"date": "2027-12-31"},
            "lastUpdatePostDateStruct": {"date": "2026-08-20"},
        },
        "designModule": {"phases": ["PHASE2", "PHASE3"], "enrollmentInfo": {"count": 984}},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "EyeBiotech Ltd."}},
        "conditionsModule": {"conditions": ["Diabetic Macular Edema (DME)"]},
    }
}

OTX = {
    "protocolSection": {
        "identificationModule": {"nctId": "NCT06495918",
                                 "briefTitle": "Study to Evaluate OTX-TKI (Axitinib)"},
        "statusModule": {"overallStatus": "ACTIVE_NOT_RECRUITING",
                         "primaryCompletionDateStruct": {"date": "2027-01-08", "type": "ESTIMATED"}},
        "designModule": {"phases": ["PHASE3"], "enrollmentInfo": {"count": 825}},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Ocular Therapeutix, Inc."}},
        "conditionsModule": {"conditions": ["Wet Age-related Macular Degeneration"]},
    }
}

SRZN_FILINGS = {
    "filings": {"recent": {
        "form": ["SCHEDULE 13G/A", "10-Q", "8-K", "4"],
        "filingDate": ["2026-08-14", "2026-08-06", "2026-08-06", "2026-07-24"],
        "items": ["", "", "2.02", ""],
        "accessionNumber": ["0000905148-26-003732", "0001193125-26-338120",
                            "0001193125-26-338068", "0001193125-26-316570"],
        "primaryDocument": ["primary_doc.xml", "srzn-20260630.htm",
                            "srzn-20260806.htm", "ownership.xml"],
    }}
}

CONFIG = {
    "clusters": [{
        "name": "Retina / Wnt pathway",
        "discovery": {"conditions": ["diabetic macular edema"],
                      "phases": ["PHASE2", "PHASE3"], "min_enrollment": 100},
        "names": [
            {"ticker": "SRZN", "cik": "0001824893", "note": "Wnt+VEGF pure-play",
             "levered_to": [{"nct": "NCT06571045", "direction": "up",
                             "why": "mechanism validation"}]},
            {"ticker": "REGN", "cik": "0000872589", "note": "Eylea incumbent",
             "levered_to": [{"nct": "NCT06571045", "direction": "down",
                             "why": "franchise threat"}]},
        ],
    }],
    "settings": {"weekly_window_days": 45, "squeeze_float_max": 15000000,
                 "squeeze_short_pct_min": 15.0,
                 "edgar_forms_watched": ["8-K"], "edgar_urgent_items": ["8.01"]},
}


def fake_fetch(url, headers=None, tries=3):
    if "filter.ids" in url:
        wanted = url.split("filter.ids=")[1].split("&")[0]
        studies = []
        if "NCT06571045" in wanted:
            studies.append(BRUNELLO)
        if "NCT06495918" in wanted:
            studies.append(OTX)
        return {"studies": studies}
    if "query.cond" in url:
        return {"studies": [BRUNELLO, OTX], "totalCount": 2}
    if "submissions" in url:
        return SRZN_FILINGS
    return None


def freeze(iso):
    """Pin date.today() so 'days until' is deterministic."""
    real = datetime.date

    class Frozen(real):
        @classmethod
        def today(cls):
            return real.fromisoformat(iso)
    cat.date = Frozen


sent = []
cat.push = lambda title, message, priority="default", tags="", click=None: sent.append(
    {"title": title, "body": message, "priority": priority, "click": click})
cat.squeeze_profile = lambda t: (
    {"float": 4_620_000, "short": 1_450_000, "short_pct": 31.29, "price": 16.16}
    if t == "SRZN" else {"float": 105_000_000, "short": 3_000_000,
                         "short_pct": 2.9, "price": 600.0})

# ---------------------------------------------------------------------------

print("Parsing the real payload shape")
row = cat.flatten_study(BRUNELLO)
check("nct id read", row["nct"] == "NCT06571045")
check("primary completion date read", row["pcd"] == "2026-09-30", str(row["pcd"]))
check("date is flagged estimated", row["pcd_type"] == "ESTIMATED")
check("phase joined", row["phase"] == "PHASE2/PHASE3", row["phase"])
check("enrollment read", row["enrollment"] == 984)
check("sponsor is the acquired subsidiary, not Merck",
      row["sponsor"] == "EyeBiotech Ltd.")

print()
print("Standing on 2026-09-01, three weeks before the readout")
freeze("2026-09-01")
sent.clear()
state = {"trials": {}, "filings": {}, "seen_discovery": []}
cat.run_weekly(CONFIG, state, fetch=fake_fetch)
check("a digest went out", len(sent) == 1)
body = sent[0]["body"] if sent else ""
check("BRUNELLO is on it", "2026-09-30" in body, body)
check("SRZN is named as levered up", "SRZN up" in body, body)
check("REGN is named as levered down", "REGN down" in body, body)
check("days-to-event shown", "(29d)" in body, body)
check("estimated dates marked with ~", "~2026-09-30" in body, body)
check("squeeze note attached under the catalyst",
      "SRZN: float 4.6M, 31% short" in body, body)
check("squeeze note omitted for the liquid name",
      "REGN: float" not in body, body)
check("the direction caveat is in every digest",
      "Timing, not direction" in body, body)

print()
print("Standing on 2026-06-01, well outside the window")
freeze("2026-06-01")
sent.clear()
cat.run_weekly(CONFIG, {"trials": {}, "filings": {}, "seen_discovery": []},
               fetch=fake_fetch)
check("nothing is sent when nothing is due", len(sent) == 0,
      sent[0]["body"] if sent else "")

print()
print("A stale server-side filter cannot flood the digest")
freeze("2026-11-01")          # BRUNELLO past, OTX still 68 days out
sent.clear()
cat.run_weekly(CONFIG, {"trials": {}, "filings": {}, "seen_discovery": []},
               fetch=fake_fetch)
check("out-of-window discovery results are dropped client-side",
      not sent or "2027-01-08" not in sent[0]["body"],
      sent[0]["body"] if sent else "")

print()
print("Daily change poll")
freeze("2026-09-24")
sent.clear()
state = {"trials": {"NCT06571045": {"status": "ACTIVE_NOT_RECRUITING",
                                    "pcd": "2026-09-30"}},
         "filings": {}, "seen_discovery": []}
cat.run_daily(CONFIG, state, fetch=fake_fetch)
check("no change means no trial alert",
      not any("NCT06571045 changed" in s["title"] for s in sent))
check("first sight of a company does not fire historical filings",
      not any("new 8-K" in s["title"] for s in sent),
      "; ".join(s["title"] for s in sent))
check("baseline recorded for SRZN", len(state["filings"].get("SRZN", [])) == 4)

sent.clear()
state["trials"]["NCT06571045"] = {"status": "RECRUITING", "pcd": "2026-12-31"}
cat.run_daily(CONFIG, state, fetch=fake_fetch)
titles = " | ".join(s["title"] for s in sent)
check("status flip alerts", any("changed" in s["title"] for s in sent), titles)
body = next((s["body"] for s in sent if "changed" in s["title"]), "")
check("the date move is spelled out",
      "completion 2026-12-31 -> 2026-09-30" in body, body)
check("change alert is high priority",
      any(s["priority"] == "high" for s in sent if "changed" in s["title"]))

sent.clear()
state["filings"]["SRZN"] = ["0001193125-26-316570"]      # only the old form 4
cat.run_daily(CONFIG, state, fetch=fake_fetch)
check("a genuinely new 8-K alerts",
      any(s["title"] == "SRZN: new 8-K" for s in sent),
      " | ".join(s["title"] for s in sent))
f8 = next((s for s in sent if s["title"] == "SRZN: new 8-K"), None)
check("filing alert links to the document",
      bool(f8) and f8["click"].endswith("srzn-20260806.htm"),
      f8["click"] if f8 else "")
check("item 2.02 is not treated as urgent",
      bool(f8) and f8["priority"] == "default", f8["priority"] if f8 else "")

print()
print("Config on disk is valid")
cfg = cat.load_config(Path(__file__).parent / "catalysts.json")
by_nct, by_ticker = cat.watched_map(cfg)
check("BRUNELLO is mapped", "NCT06571045" in by_nct)
check("SRZN and REGN both point at it",
      {"SRZN", "REGN"} <= {t for t, _ in by_nct["NCT06571045"]})
check("every mapped name has a CIK",
      all(n.get("cik") for n in by_ticker.values()),
      str([t for t, n in by_ticker.items() if not n.get("cik")]))
check("every levered_to entry says why",
      all(l.get("why") for n in by_ticker.values() for l in n["levered_to"]))

print()
print("FAILED: " + ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
