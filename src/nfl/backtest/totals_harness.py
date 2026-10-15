"""Common totals evaluation harness (Phase 2A/2B).

Three entry points, all pure functions of cached data:

  run_baseline(out_dir, dev=(2015, 2022), holdout_start=2023)
      Walk-forward Ridge totals model, UNCHANGED, on development seasons only.
      Holdout seasons are dropped from the input before anything runs, so no
      prediction for them is ever produced. Writes baseline_predictions_dev_*.csv,
      a 2010-2014 replication file, and baseline_meta.json.

  analyze(pred_csv, out_dir, label)
      Edge buckets (0-1, 1-1.5, 1.5-2, 2-2.5, 2.5-3, 3-4, 4+), Over/Under split,
      cumulative rules, season table, calibration, trend tests, bootstrap CIs.

  compare(new_pred_csv, frozen_pred_csv)
      Field-by-field reproduction check against the frozen Phase 2A files.

Periods are fixed: development 2015-2022, holdout 2023-2025 (untouched),
forward 2026 weeks 7-18. Changing them is a decision, not a parameter.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from src.nfl.data.ingest import load_cached_pbp_for_epa, load_cached_schedules
from src.nfl.features.epa_features import add_trailing_epa_features
from src.nfl.features.qb_features import add_trailing_qb_features
from src.nfl.models.total.total_model import FEATURE_COLS, MODEL_VERSION, over_actual, over_probability, walk_forward_total

DEV = (2015, 2022)
HOLDOUT_START = 2023
REPLICATION = (2010, 2014)
BREAKEVEN = 110 / 210
BUCKETS = [(0.0, 1.0, "0.00-0.99"), (1.0, 1.5, "1.00-1.49"), (1.5, 2.0, "1.50-1.99"), (2.0, 2.5, "2.00-2.49"),
           (2.5, 3.0, "2.50-2.99"), (3.0, 4.0, "3.00-3.99"), (4.0, 99.0, "4.00+")]
MASTER = {"0.00-0.99": "PASS", "1.00-1.49": "Usually PASS", "1.50-1.99": "Small possible edge",
          "2.00-2.49": "Playable edge", "2.50-2.99": "Playable edge", "3.00-3.99": "Strong edge", "4.00+": "Very strong"}
PRED_COLS = ["game_id", "season", "week", "gameday", "away_team", "home_team", "away_score", "home_score",
             "total_points", "total_line", "over_odds", "under_odds", "total_mean_pred", "total_sigma_pred",
             "over_prob", "p_pick", "edge", "abs_edge", "pick", "result", "abs_err", "market_abs_err"] + FEATURE_COLS


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def score_predictions(wf: pd.DataFrame) -> pd.DataFrame:
    wf = wf.copy()
    wf["over_prob"] = over_probability(wf["total_mean_pred"], wf["total_sigma_pred"], wf["total_line"])
    wf["over_actual"] = over_actual(wf["total_points"], wf["total_line"])
    wf["edge"] = wf["total_mean_pred"] - wf["total_line"]
    wf["abs_edge"] = wf["edge"].abs()
    wf["pick"] = np.where(wf["edge"] > 0, "OVER", np.where(wf["edge"] < 0, "UNDER", "NONE"))
    wf["p_pick"] = np.where(wf["pick"] == "OVER", wf["over_prob"], 1 - wf["over_prob"])
    diff = wf["total_points"] - wf["total_line"]
    wf["result"] = np.select([diff == 0, (wf["pick"] == "OVER") & (diff > 0), (wf["pick"] == "UNDER") & (diff < 0)],
                             ["push", "win", "win"], default="loss")
    wf.loc[wf["pick"] == "NONE", "result"] = "none"
    wf["abs_err"] = (wf["total_points"] - wf["total_mean_pred"]).abs()
    wf["market_abs_err"] = (wf["total_points"] - wf["total_line"]).abs()
    return wf


def _eval_slice(wf: pd.DataFrame, lo: int, hi: int) -> pd.DataFrame:
    return wf[(wf.game_type == "REG") & wf.total_line.notna() & wf.total_mean_pred.notna()
              & wf.total_points.notna() & (wf.season >= lo) & (wf.season <= hi)].copy()


def run_baseline(out_dir: Path, raw_dir: Path | None = None, dev: tuple[int, int] = DEV,
                 holdout_start: int = HOLDOUT_START) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    kw = {"cache_dir": raw_dir} if raw_dir else {}
    games = load_cached_schedules(**kw)
    games = games[(games.season >= 1999) & (games.season < holdout_start)].copy()
    assert games.season.max() < holdout_start, "holdout seasons must not be present"
    games["gameday"] = pd.to_datetime(games["gameday"])
    for c in ["home_score", "away_score", "total_line", "over_odds", "under_odds", "spread_line"]:
        games[c] = pd.to_numeric(games[c], errors="coerce")
    pbp = load_cached_pbp_for_epa(**kw)
    pbp = pbp[pbp.game_id.str[:4].astype(int) < holdout_start]

    g = add_trailing_epa_features(games, pbp, window=10)
    g = add_trailing_qb_features(g, pbp, window=10)
    wf = score_predictions(walk_forward_total(g, min_train_games=500, window=16))

    ev = _eval_slice(wf, *dev)
    ev[PRED_COLS].to_csv(out_dir / f"baseline_predictions_dev_{dev[0]}_{dev[1]}.csv", index=False)
    rep = _eval_slice(wf, *REPLICATION)
    rep[PRED_COLS].to_csv(out_dir / f"baseline_predictions_replication_{REPLICATION[0]}_{REPLICATION[1]}.csv", index=False)
    rd = raw_dir or Path("data/raw")
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(), "model": "repo Ridge totals model, unchanged",
            "model_version": MODEL_VERSION, "features": FEATURE_COLS, "windows": {"epa": 10, "qb": 10, "scoring": 16},
            "ridge_alpha": 10.0, "min_train_games": 500,
            "periods": {"development": list(dev), "holdout_untouched": [holdout_start, 2025], "forward": "2026 wk7-18"},
            "data": {"source": "nflreadpy", "schedules_sha256_16": _sha(rd / "schedules.parquet"),
                     "pbp_sha256_16": _sha(rd / "pbp_epa.parquet"), "games_rows": int(len(games)), "plays": int(len(pbp))},
            "eval_rows": int(len(ev)), "python": sys.version.split()[0], "pandas": pd.__version__, "numpy": np.__version__}
    (out_dir / "baseline_meta.json").write_text(json.dumps(meta, indent=2))
    return meta


# ------------------------------------------------------------------ analysis

def _wilson(w, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    p = w / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def _boot(sub, rng, reps=4000):
    dec = sub[sub.push == 0]
    if len(dec) < 2:
        return (np.nan,) * 4
    w = dec.win.to_numpy(); pnl = sub.unit_pnl.to_numpy()
    wins = w[rng.integers(0, len(w), size=(reps, len(w)))].mean(axis=1)
    rois = pnl[rng.integers(0, len(pnl), size=(reps, len(pnl)))].mean(axis=1)
    return (np.percentile(wins, 2.5), np.percentile(wins, 97.5), np.percentile(rois, 2.5), np.percentile(rois, 97.5))


def _summ(sub, rng):
    n = len(sub); w = int(sub.win.sum()); l = int(sub.loss.sum()); p = int(sub.push.sum()); dec = w + l
    winp = w / dec if dec else np.nan
    wlo, whi = _wilson(w, dec); blo, bhi, rlo, rhi = _boot(sub, rng)
    d = sub[sub.push == 0]
    z = (winp - BREAKEVEN) / math.sqrt(BREAKEVEN * (1 - BREAKEVEN) / dec) if dec else np.nan
    return {"n": n, "wins": w, "losses": l, "pushes": p, "win_pct": winp, "roi_110": sub.unit_pnl.mean() if n else np.nan,
            "avg_edge": float(sub.abs_edge.mean()) if n else np.nan, "avg_abs_error": float(sub.abs_err.mean()) if n else np.nan,
            "market_abs_error": float(sub.market_abs_err.mean()) if n else np.nan,
            "brier_over": float(((d.over_prob - d.over_outcome) ** 2).mean()) if len(d) else np.nan,
            "model_pred_win_pct": float(d.p_pick.mean()) if len(d) else np.nan,
            "calib_gap": (winp - float(d.p_pick.mean())) if dec else np.nan,
            "wilson_lo": wlo, "wilson_hi": whi, "boot_win_lo": blo, "boot_win_hi": bhi, "boot_roi_lo": rlo, "boot_roi_hi": rhi,
            "z_vs_breakeven": z, "p_vs_breakeven": (2 * (1 - stats.norm.cdf(abs(z))) if dec else np.nan)}


def analyze(pred_csv: Path, out_dir: Path, label: str, seed: int = 20261015) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    df = pd.read_csv(pred_csv)
    df = df[df.pick != "NONE"].copy()
    df["win"] = (df.result == "win").astype(int); df["loss"] = (df.result == "loss").astype(int); df["push"] = (df.result == "push").astype(int)
    df["unit_pnl"] = np.select([df.win == 1, df.loss == 1], [100 / 110, -1.0], default=0.0)
    df["over_outcome"] = np.where(df.total_points > df.total_line, 1.0, np.where(df.total_points < df.total_line, 0.0, np.nan))
    df["bucket"] = pd.cut(df.abs_edge, bins=[b[0] for b in BUCKETS] + [99.0], labels=[b[2] for b in BUCKETS], right=False)
    rows = []
    for _, _, name in BUCKETS:
        sub = df[df.bucket == name]
        rows.append({"period": label, "side": "ALL", "bucket": name, "master_system": MASTER[name], **_summ(sub, rng)})
        for side in ("OVER", "UNDER"):
            rows.append({"period": label, "side": side, "bucket": name, "master_system": MASTER[name], **_summ(sub[sub.pick == side], rng)})
    for thr in (0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
        sub = df[df.abs_edge >= thr]
        rows.append({"period": label, "side": "ALL", "bucket": f">= {thr:.1f}", "master_system": "cumulative", **_summ(sub, rng)})
        for side in ("OVER", "UNDER"):
            rows.append({"period": label, "side": side, "bucket": f">= {thr:.1f}", "master_system": "cumulative", **_summ(sub[sub.pick == side], rng)})
    B = pd.DataFrame(rows); B.to_csv(out_dir / f"buckets_{label}.csv", index=False)
    S = []
    for s, grp in df.groupby("season"):
        for name, sub in (("all", grp), (">=1.5", grp[grp.abs_edge >= 1.5]), (">=2.0", grp[grp.abs_edge >= 2.0]), (">=3.0", grp[grp.abs_edge >= 3.0])):
            r = _summ(sub, rng)
            S.append({"period": label, "season": int(s), "filter": name, "n": r["n"], "wins": r["wins"], "losses": r["losses"], "pushes": r["pushes"],
                      "win_pct": r["win_pct"], "roi_110": r["roi_110"], "model_mae": r["avg_abs_error"], "market_mae": r["market_abs_error"]})
    pd.DataFrame(S).to_csv(out_dir / f"seasons_{label}.csv", index=False)
    dec = df[df.push == 0]
    rho, rho_p = stats.spearmanr(dec.abs_edge, dec.win)
    X = np.column_stack([np.ones(len(dec)), dec.abs_edge.to_numpy()]); y = dec.win.to_numpy(); beta = np.zeros(2)
    for _ in range(50):
        pr = 1 / (1 + np.exp(-X @ beta)); Wm = pr * (1 - pr)
        step = np.linalg.solve((X * Wm[:, None]).T @ X, X.T @ (y - pr)); beta += step
        if np.abs(step).max() < 1e-8:
            break
    pr = 1 / (1 + np.exp(-X @ beta)); Wm = pr * (1 - pr); se_b = math.sqrt(np.linalg.inv((X * Wm[:, None]).T @ X)[1, 1])
    low = dec[dec.abs_edge < 1.0]; high = dec[dec.abs_edge >= 2.0]
    chi_p = stats.chi2_contingency(np.array([[high.win.sum(), len(high) - high.win.sum()], [low.win.sum(), len(low) - low.win.sum()]]))[1]
    dec = dec.assign(pbin=pd.cut(dec.p_pick, [0.5, 0.52, 0.54, 0.56, 0.58, 0.60, 1.0], right=False))
    cal = dec.groupby("pbin", observed=True).agg(n=("win", "size"), predicted=("p_pick", "mean"), realized=("win", "mean")).reset_index()
    cal["pbin"] = cal.pbin.astype(str); cal.to_csv(out_dir / f"calibration_{label}.csv", index=False)
    tests = B[B.master_system != "cumulative"]; best = tests.loc[tests.win_pct.idxmax()]
    trend = {"period": label, "n_decided": int(len(dec)), "spearman_rho_edge_vs_win": float(rho), "spearman_p": float(rho_p),
             "logit_slope_per_point": float(beta[1]), "logit_slope_se": se_b, "logit_p": float(2 * (1 - stats.norm.cdf(abs(beta[1] / se_b)))),
             "win_pct_edge_lt_1": float(low.win.mean()), "n_lt_1": int(len(low)), "win_pct_edge_ge_2": float(high.win.mean()), "n_ge_2": int(len(high)),
             "chi2_p_ge2_vs_lt1": float(chi_p), "n_bucket_side_tests": int(tests.p_vs_breakeven.notna().sum()),
             "best_bucket": f"{best.side} {best.bucket}", "best_bucket_win_pct": float(best.win_pct), "best_bucket_n": int(best.n),
             "overall_win_pct": float(df.win.sum() / (df.win.sum() + df.loss.sum())), "overall_roi_110": float(df.unit_pnl.mean()),
             "overall_model_mae": float(df.abs_err.mean()), "overall_market_mae": float(df.market_abs_err.mean()),
             "overall_brier_over": float(((dec.over_prob - dec.over_outcome) ** 2).mean()), "over_pick_share": float((df.pick == "OVER").mean())}
    (out_dir / f"trend_{label}.json").write_text(json.dumps(trend, indent=2))
    return trend


# ------------------------------------------------------------------ reproduction check

def compare(new_csv: Path, frozen_csv: Path, tol_pred: float = 1e-6) -> dict:
    """Field-by-field check that a re-run reproduces the frozen Phase 2A predictions."""
    a = pd.read_csv(new_csv).set_index("game_id").sort_index()
    b = pd.read_csv(frozen_csv).set_index("game_id").sort_index()
    out = {"new_rows": int(len(a)), "frozen_rows": int(len(b)), "games_only_in_new": sorted(set(a.index) - set(b.index)),
           "games_only_in_frozen": sorted(set(b.index) - set(a.index))}
    common = a.index.intersection(b.index); a, b = a.loc[common], b.loc[common]
    diffs = {}
    for c in FEATURE_COLS + ["total_mean_pred", "total_sigma_pred", "over_prob", "edge", "total_line", "total_points", "abs_err"]:
        d = (a[c].astype(float) - b[c].astype(float)).abs()
        diffs[c] = {"max_abs_diff": float(d.max()), "n_diff_gt_tol": int((d > tol_pred).sum())}
    out["numeric"] = diffs
    out["pick_mismatches"] = int((a.pick != b.pick).sum())
    out["result_mismatches"] = int((a.result != b.result).sum())
    dec_a = a[a.result != "push"]; dec_b = b[b.result != "push"]
    out["win_pct"] = {"new": float((dec_a.result == "win").mean()), "frozen": float((dec_b.result == "win").mean())}
    out["mae"] = {"new": float(a.abs_err.mean()), "frozen": float(b.abs_err.mean())}
    oa = np.where(a.total_points > a.total_line, 1.0, np.where(a.total_points < a.total_line, 0.0, np.nan))
    ob = np.where(b.total_points > b.total_line, 1.0, np.where(b.total_points < b.total_line, 0.0, np.nan))
    out["brier"] = {"new": float(np.nanmean((a.over_prob - oa) ** 2)), "frozen": float(np.nanmean((b.over_prob - ob) ** 2))}
    out["identical"] = (not out["games_only_in_new"] and not out["games_only_in_frozen"] and out["pick_mismatches"] == 0
                        and out["result_mismatches"] == 0 and all(v["n_diff_gt_tol"] == 0 for v in diffs.values()))
    return out
