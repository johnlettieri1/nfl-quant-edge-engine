"""Phase 3B harness: walk-forward residual candidates, preserved references, and the
walk-forward calibration layer. Spec: docs/phase3b/PHASE_3B_SPEC.md.

Holdout discipline (asserted): every input frame is built by phase3a_harness._load_dev_inputs,
which drops schedule rows >= 2023 and all 2023+ play-by-play before any feature is computed.
Context inputs (weather text, injuries, snap counts) are pulled through 2022 only, and this
module asserts no 2023+ game is present before fitting. Out-of-sample predictions are written
for every season with enough history (needed to train the walk-forward calibrators); the
evaluation slice is 2015-2022 regular season.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src.nfl.backtest.phase3a_harness import ALL, A, B, Cx, D, assemble, build_feature_frames, walk_forward_generic
from src.nfl.features import phase3a_config as C3
from src.nfl.models.residual import phase3b_config as C
from src.nfl.models.residual.calibration import no_vig_over, walk_forward_calibrate
from src.nfl.models.residual.residual_model import walk_forward_residual
from src.nfl.models.total.total_model import FEATURE_COLS

LINE = ["total_line"]
R8 = LINE + FEATURE_COLS
GROUPS_3A = {"form": A, "pace": B, "xpl": Cx, "poss": D}

# id -> (frame variant, features, select_groups or None, first_train_season)
RESIDUAL_MODELS: dict[str, tuple[str, list[str], dict | None, int]] = {
    "r0_line": ("fix", LINE, None, C.FIRST_SEASON),
    "r1_ridge8": ("fix", R8, None, C.FIRST_SEASON),
    "r1o_ridge8_origcodes": ("orig", R8, None, C.FIRST_SEASON),
    "r1_form": ("fix", R8 + A, None, C.FIRST_SEASON),
    "r1_pace": ("fix", R8 + B, None, C.FIRST_SEASON),
    "r1_xpl": ("fix", R8 + Cx, None, C.FIRST_SEASON),
    "r1_poss": ("fix", R8 + D, None, C.FIRST_SEASON),
    "r2_phase3a": ("fix", R8 + ALL, None, C.FIRST_SEASON),
    "r3_conservative": ("fix", R8, GROUPS_3A, C.FIRST_SEASON),
}

OUT_COLS = ["game_id", "season", "week", "game_type", "gameday", "away_team", "home_team", "total_points", "total_line",
            "over_odds", "under_odds", "residual", "r", "r_sigma", "over", "p_mkt", "p_raw", "p_logistic", "p_shrink",
            "p_isotonic", "model_version"]


def _frame(frames: dict, variant: str) -> pd.DataFrame:
    df = assemble(frames, "fix", C3.HALF_LIFE_PRIMARY) if variant == "fix" else frames["orig"].copy()
    assert df["season"].max() < C.HOLDOUT_START, "holdout season present"
    return df


def finalize(df: pd.DataFrame, r_col: str, sigma_col: str, model_id: str) -> pd.DataFrame:
    f = df.copy()
    if "residual" not in f:
        f["residual"] = f["total_points"] - f["total_line"]
    f["r"] = f[r_col]; f["r_sigma"] = f[sigma_col]
    f["over"] = np.where(f.residual > 0, 1.0, np.where(f.residual < 0, 0.0, np.nan))
    f.loc[f.total_points.isna(), "over"] = np.nan
    f["p_mkt"] = no_vig_over(f["over_odds"], f["under_odds"])
    f = f[f["r"].notna() & f["total_line"].notna()].copy()
    f = walk_forward_calibrate(f)
    f["model_version"] = model_id
    return f


def reference_predictions(frames: dict) -> dict[str, pd.DataFrame]:
    """ridge8_v1 (unstandardized generic walk-forward reproduces it exactly, proven in Phase 3A tests)
    and the best Phase 3A candidate p3a_c3_xpl, re-expressed as point edges r = total - line."""
    out = {}
    wf = walk_forward_generic(frames["orig"], FEATURE_COLS, standardize=False)
    wf["r_ref"] = wf["total_mean_pred"] - wf["total_line"]
    out["ridge8_v1"] = finalize(wf, "r_ref", "total_sigma_pred", "ridge8_v1")
    df = assemble(frames, "fix", C3.HALF_LIFE_PRIMARY)
    wf = walk_forward_generic(df, FEATURE_COLS + Cx, standardize=True)
    wf["r_ref"] = wf["total_mean_pred"] - wf["total_line"]
    out["p3a_c3_xpl"] = finalize(wf, "r_ref", "total_sigma_pred", "p3a_c3_xpl")
    return out


def market_zero(ref: pd.DataFrame) -> pd.DataFrame:
    """m0_market: predicted residual 0 on the same games; every probability = no-vig market."""
    f = ref.copy()
    f["r"] = 0.0
    for c in ("p_raw", "p_logistic", "p_shrink", "p_isotonic"):
        f[c] = f["p_mkt"]
    f["model_version"] = "m0_market"
    return f


def run_models(models: dict, frames: dict, extra: pd.DataFrame | None = None) -> tuple[dict, dict]:
    preds, logs = {}, {}
    for mid, (variant, feats, groups, first) in models.items():
        df = _frame(frames, variant)
        if extra is not None:
            df = df.merge(extra, on="game_id", how="left", validate="one_to_one")
        wf = walk_forward_residual(df, feats, select_groups=groups, first_train_season=first)
        preds[mid] = finalize(wf, "r_hat", "r_sigma", mid)
        logs[mid] = {"features": wf.attrs["all_features"], "first_train_season": first, "variant": variant,
                     "fit_log": wf.attrs["fit_log"], "cal_log": preds[mid].attrs.get("cal_log", {})}
    return preds, logs


def write(preds: dict, logs: dict, out_dir: Path, tag: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for mid, f in preds.items():
        assert f["season"].max() < C.HOLDOUT_START
        f[[c for c in OUT_COLS if c in f.columns]].to_csv(out_dir / f"{mid}_oos.csv", index=False)
    (out_dir / f"fit_logs_{tag}.json").write_text(json.dumps(logs, indent=1, default=float))


def run_phase3b_1(out_dir: Path, raw_dir: Path | None = None) -> dict:
    frames = build_feature_frames(raw_dir, C.HOLDOUT_START, half_lives=(C3.HALF_LIFE_PRIMARY,))
    refs = reference_predictions(frames)
    refs["m0_market"] = market_zero(refs["ridge8_v1"])
    preds, logs = run_models(RESIDUAL_MODELS, frames)
    write({**refs, **preds}, logs, out_dir, "3b1")
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(), "models": list(refs) + list(preds),
            "n_schedule_rows_used": frames["n_games"], "max_season": int(max(f.season.max() for f in preds.values())),
            "python": sys.version.split()[0], "pandas": pd.__version__, "numpy": np.__version__}
    (out_dir / "phase3b1_meta.json").write_text(json.dumps(meta, indent=2))
    return {"frames": frames, "meta": meta}
