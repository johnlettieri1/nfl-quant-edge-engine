"""Phase 3A candidate harness: builds every candidate beside the frozen ridge8_v1
baseline and writes predictions in the Phase 2A schema so totals_harness.analyze
evaluates them unchanged.

Holdout discipline (asserted, not assumed): schedule rows with season >= 2023 and
any play-by-play from 2023+ are dropped before any feature is computed, so no
holdout game can influence a development feature, fit, or prediction, and no
holdout prediction exists.

Walk-forward is identical to models/total/total_model.walk_forward_total: for
season S fit on all completed games with season < S (expanding from 1999, at least
500 training games, all game types), Ridge alpha 10, flat per-season sigma from the
training residuals (ddof=1), Normal CDF for P(over). The only fitting difference is
optional standardization (StandardScaler fit on the training fold only).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from src.nfl.backtest.totals_harness import DEV, HOLDOUT_START, PRED_COLS, REPLICATION, _eval_slice, score_predictions
from src.nfl.data.ingest import load_cached_pbp_for_epa, load_cached_schedules
from src.nfl.data.ingest_style import load_cached_style_pbp
from src.nfl.features import phase3a_config as C
from src.nfl.features.epa_features import add_trailing_epa_features
from src.nfl.features.phase3a_features import add_phase3a_features, aggregate_style_to_game_team, normalize_schedule_teams
from src.nfl.features.qb_features import add_trailing_qb_features
from src.nfl.models.total.total_model import FEATURE_COLS, build_total_features

BASE = FEATURE_COLS
A, B, Cx, D = C.GROUP_A, C.GROUP_B, C.GROUP_C, C.GROUP_D
ALL = A + B + Cx + D

# id -> (feature list, baseline variant, standardize, half-life)
CANDIDATES: dict[str, tuple[list[str], str, bool, float]] = {
    "p3a_c0_ridge8_std": (BASE, "orig", True, C.HALF_LIFE_PRIMARY),
    "p3a_c0b_ridge8_std_teamfix": (BASE, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c1_form": (BASE + A, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c2_pace": (BASE + B, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c3_xpl": (BASE + Cx, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c4_poss": (BASE + D, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c5_all": (BASE + ALL, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c5_all_minus_form": (BASE + B + Cx + D, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c5_all_minus_pace": (BASE + A + Cx + D, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c5_all_minus_xpl": (BASE + A + B + D, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c5_all_minus_poss": (BASE + A + B + Cx, "fix", True, C.HALF_LIFE_PRIMARY),
    "p3a_c1_form_h12": (BASE + A, "fix", True, C.HALF_LIFE_SENSITIVITY),
    "p3a_c5_all_h12": (BASE + ALL, "fix", True, C.HALF_LIFE_SENSITIVITY),
}


def _load_dev_inputs(raw_dir: Path | None, holdout_start: int):
    kw = {"cache_dir": raw_dir} if raw_dir else {}
    games = load_cached_schedules(**kw)
    games = games[(games.season >= 1999) & (games.season < holdout_start)].copy()
    games["gameday"] = pd.to_datetime(games["gameday"])
    for c in ["home_score", "away_score", "total_line", "over_odds", "under_odds", "spread_line"]:
        games[c] = pd.to_numeric(games[c], errors="coerce")
    pbp = load_cached_pbp_for_epa(**kw)
    pbp = pbp[pbp.game_id.str[:4].astype(int) < holdout_start]
    style = load_cached_style_pbp(**kw)
    style = style[style.game_id.str[:4].astype(int) < holdout_start]
    assert games.season.max() < holdout_start and pbp.game_id.str[:4].astype(int).max() < holdout_start
    assert style.game_id.str[:4].astype(int).max() < holdout_start
    return games, pbp, style


def build_feature_frames(raw_dir: Path | None = None, holdout_start: int = HOLDOUT_START,
                         half_lives: tuple[float, ...] = (C.HALF_LIFE_PRIMARY, C.HALF_LIFE_SENSITIVITY)) -> dict:
    games, pbp, style = _load_dev_inputs(raw_dir, holdout_start)
    # baseline features exactly as ridge8_v1 builds them
    g0 = add_trailing_qb_features(add_trailing_epa_features(games, pbp, window=10), pbp, window=10)
    orig = build_total_features(g0, window=16)
    # baseline features on franchise-normalized codes (amendment A1)
    gn = normalize_schedule_teams(games)
    g1 = add_trailing_qb_features(add_trailing_epa_features(gn, pbp, window=10), pbp, window=10)
    fix = build_total_features(g1, window=16)
    game_team = aggregate_style_to_game_team(style, games)
    p3 = {h: add_phase3a_features(gn, game_team, half_life=h) for h in half_lives}
    return {"orig": orig, "fix": fix, "p3a": p3, "game_team": game_team, "n_games": int(len(games)), "n_style_plays": int(len(style))}


def walk_forward_generic(df: pd.DataFrame, features: list[str], standardize: bool = True,
                         alpha: float = C.RIDGE_ALPHA, min_train_games: int = C.MIN_TRAIN_GAMES) -> pd.DataFrame:
    df = df.sort_values(["season", "week", "gameday", "game_id"]).reset_index(drop=True)
    mean_pred = pd.Series(np.nan, index=df.index); sigma_pred = pd.Series(np.nan, index=df.index)
    coefs = {}
    for season in sorted(df["season"].unique()):
        train = df[(df["season"] < season) & df["total_points"].notna()].dropna(subset=features)
        if len(train) < min_train_games:
            continue
        test = df.loc[df.index[df["season"] == season]].dropna(subset=features)
        if len(test) == 0:
            continue
        X = train[features].values; Xt = test[features].values
        if standardize:
            sc = StandardScaler().fit(X); X = sc.transform(X); Xt = sc.transform(Xt)
        m = Ridge(alpha=alpha).fit(X, train["total_points"].values)
        resid = train["total_points"].values - m.predict(X)
        mean_pred.loc[test.index] = m.predict(Xt)
        sigma_pred.loc[test.index] = float(np.std(resid, ddof=1))
        coefs[int(season)] = dict(zip(features, map(float, m.coef_)))
    out = df.copy(); out["total_mean_pred"] = mean_pred; out["total_sigma_pred"] = sigma_pred
    out.attrs["coefs"] = coefs
    return out


def assemble(frames: dict, base_variant: str, half_life: float) -> pd.DataFrame:
    base = frames[base_variant]
    p3 = frames["p3a"][half_life]
    extra = p3[["game_id"] + ALL + ["exp_poss_home", "exp_poss_away"]]
    df = base.merge(extra, on="game_id", how="left", validate="one_to_one")
    return df


def run_candidates(out_dir: Path, raw_dir: Path | None = None, holdout_start: int = HOLDOUT_START,
                   dev: tuple[int, int] = DEV, candidates: dict | None = None, frames: dict | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    cands = candidates or CANDIDATES
    frames = frames or build_feature_frames(raw_dir, holdout_start)
    summary = {}
    for cid, (feats, variant, std, hl) in cands.items():
        df = assemble(frames, variant, hl)
        wf = score_predictions(walk_forward_generic(df, feats, standardize=std))
        coefs = wf.attrs.get("coefs", {})
        # restore schedule-of-the-day team codes for output (franchise-normalized inside)
        orig_codes = frames["orig"][["game_id", "home_team", "away_team"]]
        wf = wf.drop(columns=["home_team", "away_team"]).merge(orig_codes, on="game_id", how="left")
        extra_cols = [c for c in feats if c not in PRED_COLS]
        if "exp_game_possessions" in feats:
            extra_cols += ["exp_poss_home", "exp_poss_away"]
        for lo, hi, tag in ((dev[0], dev[1], f"dev_{dev[0]}_{dev[1]}"), (REPLICATION[0], REPLICATION[1], f"replication_{REPLICATION[0]}_{REPLICATION[1]}")):
            ev = _eval_slice(wf, lo, hi)
            ev = ev.assign(model_version=cid)
            ev[PRED_COLS + extra_cols + ["model_version"]].to_csv(out_dir / f"{cid}_predictions_{tag}.csv", index=False)
        (out_dir / f"{cid}_coefficients.json").write_text(json.dumps(coefs, indent=1))
        summary[cid] = {"features": feats, "baseline_variant": variant, "standardize": std, "half_life": hl,
                        "n_features": len(feats)}
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(), "candidates": summary,
            "periods": {"development": list(dev), "holdout_untouched": [holdout_start, 2025]},
            "n_schedule_rows_used": frames["n_games"], "n_style_plays_used": frames["n_style_plays"],
            "python": sys.version.split()[0], "pandas": pd.__version__, "numpy": np.__version__}
    (out_dir / "phase3a_meta.json").write_text(json.dumps(meta, indent=2))
    return meta
