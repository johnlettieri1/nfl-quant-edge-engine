"""Impact of the relocated-team alias fix on the frozen Phase 2A (ridge8_v1) results.

Runs the ridge8_v1 model EXACTLY as frozen (same 8 features, windows, unstandardized
Ridge alpha 10, flat sigma, Normal CDF, expanding walk-forward from 1999), once on
schedule codes as given (must reproduce the frozen file) and once with relocated-team
codes normalized. Development 2015-2022 only; schedule rows >= 2023 are dropped first.
Nothing here changes ridge8_v1 or the live path; it only measures the difference.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.nfl.backtest.phase3a_eval import extended_metrics
from src.nfl.backtest.totals_harness import DEV, HOLDOUT_START, PRED_COLS, _eval_slice, analyze, compare, score_predictions
from src.nfl.data.ingest import load_cached_pbp_for_epa, load_cached_schedules
from src.nfl.data.team_aliases import RELOCATED_TEAM_ALIASES, normalize_schedule_team_codes
from src.nfl.features.epa_features import add_trailing_epa_features
from src.nfl.features.qb_features import add_trailing_qb_features
from src.nfl.models.total.total_model import walk_forward_total

HEADLINE = ("games_evaluated", "win_pct", "roi_110", "total_mae", "market_mae", "brier_over", "log_loss_over", "ece",
            "slope", "slope_p", "spearman_rho", "over_pick_share")


def _run(games, pbp):
    g = add_trailing_qb_features(add_trailing_epa_features(games, pbp, window=10), pbp, window=10)
    return _eval_slice(score_predictions(walk_forward_total(g, min_train_games=500, window=16)), *DEV)


def run_alias_fix_check(out_dir: Path, frozen_dir: Path, raw_dir: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    kw = {"cache_dir": raw_dir} if raw_dir else {}
    games = load_cached_schedules(**kw)
    games = games[(games.season >= 1999) & (games.season < HOLDOUT_START)].copy()
    games["gameday"] = pd.to_datetime(games["gameday"])
    for c in ["home_score", "away_score", "total_line", "over_odds", "under_odds", "spread_line"]:
        games[c] = pd.to_numeric(games[c], errors="coerce")
    pbp = load_cached_pbp_for_epa(**kw)
    pbp = pbp[pbp.game_id.str[:4].astype(int) < HOLDOUT_START]
    assert games.season.max() < HOLDOUT_START

    asis = _run(games, pbp)
    fixed = _run(normalize_schedule_team_codes(games), pbp)
    # output keeps the schedule-of-the-day codes so files line up with the frozen one
    fixed = fixed.drop(columns=["home_team", "away_team"]).merge(asis[["game_id", "home_team", "away_team"]], on="game_id")
    pa, pf = out_dir / "ridge8_v1_asis_dev_2015_2022.csv", out_dir / "ridge8_v1_aliasfix_dev_2015_2022.csv"
    asis[PRED_COLS].to_csv(pa, index=False); fixed[PRED_COLS].to_csv(pf, index=False)
    frozen = frozen_dir / "baseline_predictions_dev_2015_2022.csv"

    repro = compare(pa, frozen)
    m0, m1 = extended_metrics(frozen), extended_metrics(pf)
    analyze(pf, out_dir, "ridge8_v1_aliasfix_dev_2015_2022")
    a = asis.set_index("game_id"); f = fixed.set_index("game_id").loc[a.index]
    alias_game = a.home_team.isin(RELOCATED_TEAM_ALIASES) | a.away_team.isin(RELOCATED_TEAM_ALIASES)
    feat_changed = (a[["home_epa_matchup", "away_epa_matchup", "home_off_trailing", "away_off_trailing",
                       "home_def_trailing", "away_def_trailing"]] - f[["home_epa_matchup", "away_epa_matchup", "home_off_trailing",
                       "away_off_trailing", "home_def_trailing", "away_def_trailing"]]).abs().max(axis=1) > 1e-12
    res = {
        "asis_reproduces_frozen_phase2a": bool(repro["identical"]),
        "dev_games": int(len(a)),
        "dev_games_with_alias_team": int(alias_game.sum()),
        "dev_games_with_changed_features": int(feat_changed.sum()),
        "games_with_changed_pick": int((a.pick != f.pick).sum()),
        "projection_change_abs_mean": float((a.total_mean_pred - f.total_mean_pred).abs().mean()),
        "projection_change_abs_max": float((a.total_mean_pred - f.total_mean_pred).abs().max()),
        "frozen_phase2a": {k: m0[k] for k in HEADLINE},
        "with_alias_fix": {k: m1[k] for k in HEADLINE},
        "difference": {k: (m1[k] - m0[k]) for k in HEADLINE if k != "games_evaluated"},
    }
    (out_dir / "alias_fix_impact.json").write_text(json.dumps(res, indent=2, default=float))
    return res
