#!/usr/bin/env python3
"""Cross-platform command interface for the NFL totals engine (Phase 2B).

Works the same on Windows, macOS, Linux, GitHub Actions, and Claude Code:
no Makefile, no shell features, only Python and pathlib. Run from anywhere;
the script changes into the repo root so relative data/ paths resolve.

  python run.py data [--first-season 1999] [--last-season N] [--force] [--schedules-only]
      Refresh schedules; incrementally refresh play-by-play (frozen seasons are never re-pulled).
  python run.py baseline [--out data/processed/baseline] [--compare-to DIR]
      Reproduce the Phase 2A Ridge baseline on development seasons (2015-2022) and
      analyze it. --compare-to checks field-by-field against the frozen Phase 2A files.
  python run.py analyze PRED_CSV --label NAME [--out DIR]
      Bucket/side/season/calibration/trend analysis of any predictions file.
  python run.py odds [--tag opener|current|pregame]
      Take a per-book totals snapshot from The Odds API (needs ODDS_API_KEY in .env).
  python run.py predict [--season S --week W]
      Totals-only immutable weekly snapshot.
  python run.py record-results
      Settle finished games into data/predictions/totals_ledger.csv with CLV.
  python run.py ledger
      Print a ledger summary.
  python run.py test [--leakage-only]
      Run the test suite.
  python run.py data-style [--first-season 1999] [--last-season 2022]
      Phase 3A: extended play-by-play cache (pace, PROE, drives, explosives). Defaults stop at 2022
      so the 2023-2025 holdout is never downloaded for research.
  python run.py phase3b [--out data/processed/phase3b] [--skip-build]
      Phase 3B: walk-forward market-residual candidates + walk-forward calibration on 2015-2022,
      beside ridge8_v1, p3a_c3_xpl and the zero-residual market model. Holdout never loaded.
  python run.py data-context
      Phase 3B-3 inputs (weather text, injuries, snap counts, player ids), through 2022 only.
  python run.py phase3b --context
      Phase 3B-3: weather, injury and QB groups as residual predictors (after 3B-1).
  python run.py calibration-fit
      Rebuild the frozen display calibration (src/nfl/models/total/calibration_v1.json) from ridge8_v1
      walk-forward predictions on seasons <= 2022 only. Not part of the weekly path.
  python run.py alias-check --frozen DIR [--out data/processed/alias_fix]
      Data-quality check: ridge8_v1 as frozen vs the same model with relocated-team codes
      (STL/SD/OAK) normalized, on 2015-2022. Measures only; changes nothing.
  python run.py phase3a [--out data/processed/phase3a] [--frozen DIR]
      Phase 3A: build every candidate beside ridge8_v1 on 2015-2022, then evaluate against the
      frozen Phase 2A predictions in DIR (default: the in-repo baseline re-run).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(message)s")


def cmd_data(a):
    from src.nfl.data.ingest import current_season, fetch_schedules, refresh_all
    if a.schedules_only:
        df = fetch_schedules()
        print(json.dumps({"schedules_rows": int(len(df))}))
        return
    print(json.dumps(refresh_all(a.first_season, a.last_season or current_season(), force=a.force)))


def cmd_baseline(a):
    from src.nfl.backtest.totals_harness import analyze, compare, run_baseline
    out = Path(a.out)
    meta = run_baseline(out)
    print(json.dumps({"eval_rows": meta["eval_rows"], "data": meta["data"]}))
    dev = out / "baseline_predictions_dev_2015_2022.csv"
    analyze(dev, out, "dev_2015_2022")
    analyze(out / "baseline_predictions_replication_2010_2014.csv", out, "rep_2010_2014")
    if a.compare_to:
        frozen = Path(a.compare_to) / "baseline_predictions_dev_2015_2022.csv"
        res = compare(dev, frozen)
        (out / "reproduction_check.json").write_text(json.dumps(res, indent=2))
        print(json.dumps({k: res[k] for k in ("new_rows", "frozen_rows", "pick_mismatches", "result_mismatches", "win_pct", "mae", "brier", "identical")}, indent=1))
        if not res["identical"]:
            print("REPRODUCTION CHECK: DIFFERENCES FOUND -- see reproduction_check.json", file=sys.stderr)
            sys.exit(2)
        print("REPRODUCTION CHECK: identical to frozen Phase 2A baseline")


def cmd_analyze(a):
    from src.nfl.backtest.totals_harness import analyze
    print(json.dumps(analyze(Path(a.pred_csv), Path(a.out), a.label), indent=1))


def cmd_odds(a):
    from src.nfl.data.ingest import load_cached_schedules
    from src.nfl.data.odds_snapshot import take_snapshot
    sched = None
    try:
        sched = load_cached_schedules()
    except FileNotFoundError:
        logging.warning("No cached schedules; snapshot rows will not carry game_id. Run `python run.py data --schedules-only`.")
    print(json.dumps(take_snapshot(tag=a.tag, schedules=sched)))


def cmd_predict(a):
    from src.nfl.reporting.predict_totals import generate
    print(generate(a.season, a.week))


def cmd_record_results(a):
    from src.nfl.data.ingest import fetch_schedules
    from src.nfl.reporting.ledger import record_results, summarize
    sched = fetch_schedules()
    n = record_results(sched)
    print(json.dumps({"appended": n, **summarize()}, indent=1))


def cmd_ledger(a):
    from src.nfl.reporting.ledger import summarize
    print(json.dumps(summarize(), indent=1))


def cmd_test(a):
    target = "tests/leakage" if a.leakage_only else "tests"
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", target, "-q"]))


def cmd_data_style(a):
    from src.nfl.data.ingest_style import fetch_style_pbp
    df = fetch_style_pbp(a.first_season, a.last_season, force=a.force)
    print(json.dumps({"style_rows": int(len(df)), "seasons": [a.first_season, a.last_season]}))


def cmd_phase3a(a):
    from src.nfl.backtest.phase3a_eval import evaluate_all
    from src.nfl.backtest.phase3a_harness import CANDIDATES, run_candidates
    out = Path(a.out)
    if not a.skip_build:
        run_candidates(out / "predictions")
    frozen = Path(a.frozen) if a.frozen else Path("data/processed/baseline")
    res = evaluate_all(out / "predictions", frozen / "baseline_predictions_dev_2015_2022.csv", out / "analysis",
                       list(CANDIDATES), ref_rep_csv=frozen / "baseline_predictions_replication_2010_2014.csv")
    rows = []
    for cid, r in res.items():
        m = r["metrics"]
        rows.append({"model": cid, "brier": round(m["brier_over"], 4), "ece": round(m["ece"], 4), "slope": round(m["slope"], 4),
                     "slope_p": round(m["slope_p"], 3), "win": round(m["win_pct"], 4),
                     "advance": r.get("keep_rule", {}).get("advance", "-")})
    print(json.dumps(rows, indent=1))


def cmd_phase3b(a):
    from src.nfl.backtest.phase3b_eval import evaluate_dir
    from src.nfl.backtest.phase3b_harness import RESIDUAL_MODELS, run_phase3b_1
    out = Path(a.out)
    if not a.skip_build and not a.context:
        run_phase3b_1(out / "predictions")
    from src.nfl.backtest.phase3b_harness import CONTEXT_MODELS, run_phase3b_3
    if a.context:
        if not a.skip_build:
            run_phase3b_3(out / "predictions")
        ids = ["m0_market", "ridge8_v1", "r0_line", "r1_ridge8"] + list(CONTEXT_MODELS)
        res = evaluate_dir(out / "predictions", ids, out / "phase3b3_results.json")
    else:
        ids = ["m0_market", "ridge8_v1", "p3a_c3_xpl"] + list(RESIDUAL_MODELS)
        res = evaluate_dir(out / "predictions", ids, out / "phase3b1_results.json")
    rows = []
    for m in ids:
        r = res[m]; rl = r["residual"]; pl = r["prob"][r["calibrator_chosen"]]
        rows.append({"model": m, "pearson": round(rl.get("pearson", float("nan")), 4), "r2": round(rl["oos_r2"], 5),
                     "win_slope_p": round(rl.get("win_slope_p", float("nan")), 3), "cal": r["calibrator_chosen"],
                     "brier": round(pl["brier"], 5), "ece_pick": round(pl["ece_pick"], 4), "advance": r["keep_rule"]["advance"]})
    print(json.dumps(rows, indent=1))


def cmd_data_context(a):
    from src.nfl.data.ingest_context import fetch_context
    print(json.dumps(fetch_context()))


def cmd_calibration_fit(a):
    from src.nfl.models.total.fit_calibration import build
    print(json.dumps(build(), indent=1))


def cmd_alias_check(a):
    from src.nfl.backtest.alias_fix_check import run_alias_fix_check
    res = run_alias_fix_check(Path(a.out), Path(a.frozen))
    print(json.dumps(res, indent=1, default=float))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("data"); s.add_argument("--first-season", type=int, default=1999); s.add_argument("--last-season", type=int)
    s.add_argument("--force", action="store_true"); s.add_argument("--schedules-only", action="store_true"); s.set_defaults(fn=cmd_data)
    s = sub.add_parser("baseline"); s.add_argument("--out", default="data/processed/baseline"); s.add_argument("--compare-to"); s.set_defaults(fn=cmd_baseline)
    s = sub.add_parser("analyze"); s.add_argument("pred_csv"); s.add_argument("--label", required=True); s.add_argument("--out", default="data/processed/analysis"); s.set_defaults(fn=cmd_analyze)
    s = sub.add_parser("odds"); s.add_argument("--tag", default="current", choices=["opener", "current", "pregame"]); s.set_defaults(fn=cmd_odds)
    s = sub.add_parser("predict"); s.add_argument("--season", type=int); s.add_argument("--week", type=int); s.set_defaults(fn=cmd_predict)
    s = sub.add_parser("record-results"); s.set_defaults(fn=cmd_record_results)
    s = sub.add_parser("ledger"); s.set_defaults(fn=cmd_ledger)
    s = sub.add_parser("data-style"); s.add_argument("--first-season", type=int, default=1999); s.add_argument("--last-season", type=int, default=2022)
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_data_style)
    s = sub.add_parser("phase3a"); s.add_argument("--out", default="data/processed/phase3a"); s.add_argument("--frozen"); s.add_argument("--skip-build", action="store_true"); s.set_defaults(fn=cmd_phase3a)
    s = sub.add_parser("phase3b"); s.add_argument("--out", default="data/processed/phase3b"); s.add_argument("--skip-build", action="store_true")
    s.add_argument("--context", action="store_true", help="3B-3 weather/injury/QB groups (run after 3B-1)"); s.set_defaults(fn=cmd_phase3b)
    s = sub.add_parser("data-context"); s.set_defaults(fn=cmd_data_context)
    s = sub.add_parser("calibration-fit"); s.set_defaults(fn=cmd_calibration_fit)
    s = sub.add_parser("alias-check"); s.add_argument("--frozen", required=True); s.add_argument("--out", default="data/processed/alias_fix"); s.set_defaults(fn=cmd_alias_check)
    s = sub.add_parser("test"); s.add_argument("--leakage-only", action="store_true"); s.set_defaults(fn=cmd_test)
    a = p.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
