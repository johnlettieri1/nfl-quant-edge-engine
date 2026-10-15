"""One-off builder for the frozen calibration artifact (calibration_v1.json).

Inputs are restricted to seasons <= 2022 BEFORE anything is computed (asserted), so the 2023-2025
holdout never influences the factor. The probabilities calibrated are ridge8_v1's own walk-forward
out-of-sample P(over) (fit on seasons < S, predict S), exactly as in the frozen Phase 2A baseline,
scored against the closing total. All game types with a closing total and a final score; pushes excluded.

Run: python run.py calibration-fit [--out PATH]. The weekly path never runs this; it only reads the JSON.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src.nfl.data.ingest import load_cached_pbp_for_epa, load_cached_schedules
from src.nfl.features.epa_features import add_trailing_epa_features
from src.nfl.features.qb_features import add_trailing_qb_features
from src.nfl.models.total.calibration import ARTIFACT, CALIBRATION_MAX_TRAINING_SEASON, fit_shrink_factor, validate
from src.nfl.models.total.total_model import MODEL_VERSION, over_probability, walk_forward_total

VERSION = "ridge8_v1_shrink_cal_v1"


def oos_frame(last_season: int = CALIBRATION_MAX_TRAINING_SEASON) -> pd.DataFrame:
    if last_season > CALIBRATION_MAX_TRAINING_SEASON:
        from src.nfl.models.total.calibration import CalibrationHoldoutError
        raise CalibrationHoldoutError(f"refusing to build calibration data past {CALIBRATION_MAX_TRAINING_SEASON}")
    games = load_cached_schedules()
    games = games[(games.season >= 1999) & (games.season <= last_season)].copy()
    games["gameday"] = pd.to_datetime(games["gameday"])
    for c in ["home_score", "away_score", "total_line", "over_odds", "under_odds", "spread_line"]:
        games[c] = pd.to_numeric(games[c], errors="coerce")
    pbp = load_cached_pbp_for_epa()
    pbp = pbp[pbp.game_id.str[:4].astype(int) <= last_season]
    assert games.season.max() <= last_season and pbp.game_id.str[:4].astype(int).max() <= last_season
    g = add_trailing_qb_features(add_trailing_epa_features(games, pbp, window=10), pbp, window=10)
    wf = walk_forward_total(g, min_train_games=500, window=16)
    wf = wf[wf.total_mean_pred.notna() & wf.total_line.notna() & wf.total_points.notna()].copy()
    wf["p_raw"] = over_probability(wf.total_mean_pred, wf.total_sigma_pred, wf.total_line)
    diff = wf.total_points - wf.total_line
    wf = wf[diff != 0].copy()
    wf["over"] = (wf.total_points > wf.total_line).astype(float)
    return wf


def build(out: Path = ARTIFACT) -> dict:
    wf = oos_frame()
    factor = fit_shrink_factor(wf.p_raw.to_numpy(), wf.over.to_numpy(), wf.season.to_numpy())
    p_cal = 0.5 + factor * (wf.p_raw - 0.5)
    ll = lambda p: float(-(wf.over * np.log(p) + (1 - wf.over) * np.log(1 - p)).mean())
    sched = Path("data/raw/schedules.parquet")
    meta = {
        "calibration_version": VERSION, "model_version": MODEL_VERSION, "method": "shrink_toward_0.5",
        "formula": "calibrated_probability = 0.5 + shrink_factor * (raw_model_probability - 0.5)",
        "shrink_factor": round(factor, 4),
        "training_seasons": [int(wf.season.min()), int(wf.season.max())], "training_rows": int(len(wf)),
        "training_source": "ridge8_v1 walk-forward out-of-sample P(over) vs closing total; all game types; pushes excluded",
        "calibration_target": "closing-total outcomes (actual total vs historical closing total)",
        "historical_market_timing": "close",
        "live_market_timing": "snapshot line at prediction time (opener, current or pregame)",
        "timing_validation_status": "not yet prospectively validated for opener/current/pregame timing",
        "objective": "minimize log loss, factor bounded to [0, 1]",
        "holdout_policy": f"no season after {CALIBRATION_MAX_TRAINING_SEASON} used to fit, adjust, validate or tune",
        "in_sample_fit_check": {"log_loss_raw": round(ll(wf.p_raw), 5), "log_loss_calibrated": round(ll(p_cal), 5),
                                "brier_raw": round(float(((wf.p_raw - wf.over) ** 2).mean()), 5),
                                "brier_calibrated": round(float(((p_cal - wf.over) ** 2).mean()), 5)},
        "reference": "docs/phase3b/PHASE_3B_REPORT.md sections 4 and 9 (walk-forward shrink, 2015-2022)",
        "fitted_at": datetime.now(timezone.utc).isoformat(),
        "schedules_sha256_16": hashlib.sha256(sched.read_bytes()).hexdigest()[:16] if sched.exists() else None,
    }
    validate(meta)
    out.write_text(json.dumps(meta, indent=2) + "\n")
    return meta
