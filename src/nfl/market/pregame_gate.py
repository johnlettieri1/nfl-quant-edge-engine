"""Pregame planner gate (standard library only; runs every 10 minutes in game windows).

    python -m src.nfl.market.pregame_gate --raw-list raw_names.txt [--github-output $GITHUB_OUTPUT]

Calls only the FREE /events endpoint, then decides (timing.plan_pregame) whether a paid pull is
needed now. Never spends a credit itself. Network errors never print the request URL.
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

from src.nfl.market.timing import parse_utc, plan_pregame, pulls_this_week

EVENTS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events"


def fetch_kickoffs() -> list[datetime]:
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
    return [parse_utc(e["commence_time"]) for e in events]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-list", required=True, help="file with one records raw filename per line")
    ap.add_argument("--github-output")
    a = ap.parse_args(argv)
    names = [ln.strip() for ln in open(a.raw_list) if ln.strip()]
    now = datetime.now(timezone.utc)
    d = plan_pregame(now, fetch_kickoffs(), names, pulls_this_week(now, names))
    print(f"pregame gate: pull={d.pull} reason={d.reason} targets={d.target_kickoffs}")
    if a.github_output:
        with open(a.github_output, "a") as f:
            f.write(f"pull={'true' if d.pull else 'false'}\n")
            f.write(f"targets={json.dumps(d.target_kickoffs)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
