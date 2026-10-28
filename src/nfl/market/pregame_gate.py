"""Pregame planner gate (standard library only; runs every 10 minutes in game windows).

    python -m src.nfl.market.pregame_gate --raw-list raw_names.txt --odds-dir rec/data/odds \
        [--github-output $GITHUB_OUTPUT]

Calls only the FREE /events endpoint, reads which games were actually quoted from the records
paired-quote files, then decides game by game (timing.plan_pregame) whether a paid pull is needed
now. Never spends a credit itself. Network errors never print the request URL.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from src.nfl.market.timing import coverage_from_quote_csvs, credits_this_week, parse_utc, plan_pregame, quote_files

EVENTS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events"


def fetch_events() -> list[tuple[str, datetime]]:
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise SystemExit("ODDS_API_KEY not set")
    req = urllib.request.Request(EVENTS_URL + "?" + urllib.parse.urlencode({"apiKey": key, "dateFormat": "iso"}),
                                 headers={"User-Agent": "nfl-quant-edge-engine-pregame-gate"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            events = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError) as e:
        raise SystemExit(f"/events request failed: {type(e).__name__}") from None
    return [(str(e["id"]), parse_utc(e["commence_time"])) for e in events]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-list", required=True, help="file with one records raw filename per line")
    ap.add_argument("--odds-dir", required=True, help="records data/odds checkout (paired-quote files)")
    ap.add_argument("--github-output")
    a = ap.parse_args(argv)
    names = [ln.strip() for ln in open(a.raw_list) if ln.strip()]
    now = datetime.now(timezone.utc)
    cov = coverage_from_quote_csvs(quote_files(a.odds_dir))
    d = plan_pregame(now, fetch_events(), names, credits_this_week(now, names, a.odds_dir), coverage=cov)
    print(f"pregame gate: pull={d.pull} reason={d.reason} targets={d.targets}")
    if a.github_output:
        with open(a.github_output, "a") as f:
            f.write(f"pull={'true' if d.pull else 'false'}\n")
            f.write(f"targets={json.dumps(d.targets)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
