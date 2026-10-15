"""Phase 3A evaluation: the Phase 2A analysis (totals_harness.analyze) plus the
extra measures the Phase 3A brief requires, and the pre-registered keep rule.

Extra measures (definitions fixed in docs/phase3a/PHASE_3A_FEATURE_SPEC.md section 8):
  log loss on P(over); ECE over the Phase 2A p_pick bins; logistic slope of win on |edge|
  with Wald p-value and 95% CI plus a bootstrap CI; slope by season, by side, and
  leave-one-season-out; high-confidence (>= 56%) calibration gap; paired bootstrap
  differences vs ridge8_v1 (Brier, log loss, model MAE) on the identical game set.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from src.nfl.backtest.totals_harness import analyze

PBINS = [0.5, 0.52, 0.54, 0.56, 0.58, 0.60, 1.0001]
BOOT_REPS = 4000


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df.pick != "NONE"].copy()
    df["win"] = (df.result == "win").astype(int)
    df["push"] = (df.result == "push").astype(int)
    df["over_outcome"] = np.where(df.total_points > df.total_line, 1.0, np.where(df.total_points < df.total_line, 0.0, np.nan))
    return df


def logit_slope(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Return (slope, se, two-sided p) of win ~ 1 + |edge| by IRLS (same as the Phase 2A harness)."""
    X = np.column_stack([np.ones(len(x)), x]); beta = np.zeros(2)
    for _ in range(100):
        pr = 1 / (1 + np.exp(-X @ beta)); W = pr * (1 - pr)
        step = np.linalg.solve((X * W[:, None]).T @ X, X.T @ (y - pr)); beta += step
        if np.abs(step).max() < 1e-10:
            break
    pr = 1 / (1 + np.exp(-X @ beta)); W = pr * (1 - pr)
    se = math.sqrt(np.linalg.inv((X * W[:, None]).T @ X)[1, 1])
    return float(beta[1]), se, float(2 * (1 - stats.norm.cdf(abs(beta[1] / se))))


def ece(dec: pd.DataFrame) -> tuple[float, pd.DataFrame]:
    b = pd.cut(dec.p_pick, PBINS, right=False)
    t = dec.groupby(b, observed=True).agg(n=("win", "size"), stated=("p_pick", "mean"), realized=("win", "mean")).reset_index()
    t["gap"] = t.realized - t.stated
    return float((t.n / t.n.sum() * t.gap.abs()).sum()), t


def extended_metrics(pred_csv: Path, seed: int = 20261015) -> dict:
    rng = np.random.default_rng(seed)
    df = _prep(pd.read_csv(pred_csv))
    dec = df[df.push == 0]
    po = dec.over_prob.clip(1e-6, 1 - 1e-6)
    brier = float(((dec.over_prob - dec.over_outcome) ** 2).mean())
    logloss = float(-(dec.over_outcome * np.log(po) + (1 - dec.over_outcome) * np.log(1 - po)).mean())
    e, cal = ece(dec)
    hi = cal[cal.stated >= 0.56]
    hi_gap = float((hi.n / hi.n.sum() * hi.gap.abs()).sum()) if len(hi) else float("nan")
    slope, se, p = logit_slope(dec.abs_edge.to_numpy(), dec.win.to_numpy())
    x = dec.abs_edge.to_numpy(); y = dec.win.to_numpy(); bs = []
    for _ in range(1000):
        idx = rng.integers(0, len(x), len(x)); bs.append(logit_slope(x[idx], y[idx])[0])
    rho, rho_p = stats.spearmanr(dec.abs_edge, dec.win)
    by_season = {}
    for s, g in dec.groupby("season"):
        b_, se_, p_ = logit_slope(g.abs_edge.to_numpy(), g.win.to_numpy())
        by_season[int(s)] = {"slope": b_, "p": p_, "n": int(len(g)), "win_pct": float(g.win.mean())}
    loso = {int(s): logit_slope(dec[dec.season != s].abs_edge.to_numpy(), dec[dec.season != s].win.to_numpy())[0]
            for s in sorted(dec.season.unique())}
    by_side = {}
    for side in ("OVER", "UNDER"):
        g = dec[dec.pick == side]
        b_, se_, p_ = logit_slope(g.abs_edge.to_numpy(), g.win.to_numpy())
        by_side[side] = {"slope": b_, "p": p_, "n": int(len(g)), "win_pct": float(g.win.mean())}
    return {"games_evaluated": int(len(df)), "decided": int(len(dec)), "total_mae": float(df.abs_err.mean()),
            "market_mae": float(df.market_abs_err.mean()), "brier_over": brier, "log_loss_over": logloss,
            "ece": e, "hi_conf_gap_ge56": hi_gap, "hi_conf_n": int(hi.n.sum()) if len(hi) else 0,
            "spearman_rho": float(rho), "spearman_p": float(rho_p),
            "slope": slope, "slope_se": se, "slope_p": p, "slope_ci_wald": [slope - 1.96 * se, slope + 1.96 * se],
            "slope_ci_boot": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))],
            "slope_by_season": by_season, "slope_loso": loso, "slope_by_side": by_side,
            "win_pct": float(dec.win.mean()), "roi_110": float(np.where(df.win == 1, 100 / 110, np.where(df.push == 1, 0.0, -1.0)).mean()),
            "over_pick_share": float((df.pick == "OVER").mean()), "mean_abs_edge": float(df.abs_edge.mean()),
            "sd_edge": float(df.edge.std()), "calibration": cal.assign(pbin=cal.pbin.astype(str)).to_dict(orient="records")}


def paired_vs_reference(cand_csv: Path, ref_csv: Path, seed: int = 20261015) -> dict:
    a = _prep(pd.read_csv(cand_csv)).set_index("game_id"); b = _prep(pd.read_csv(ref_csv)).set_index("game_id")
    common = a.index.intersection(b.index); a, b = a.loc[common], b.loc[common]
    ok = a.over_outcome.notna().to_numpy()

    def ll(p, o):
        p = np.clip(p, 1e-6, 1 - 1e-6); return -(o * np.log(p) + (1 - o) * np.log(1 - p))
    d_brier = ((a.over_prob - a.over_outcome) ** 2 - (b.over_prob - b.over_outcome) ** 2).to_numpy()[ok]
    d_ll = (ll(a.over_prob, a.over_outcome) - ll(b.over_prob, b.over_outcome)).to_numpy()[ok]
    d_mae = (a.abs_err - b.abs_err).to_numpy()
    rng = np.random.default_rng(seed); out = {"n_common": int(len(common))}
    for name, d in (("brier", d_brier), ("log_loss", d_ll), ("mae", d_mae)):
        bs = d[rng.integers(0, len(d), size=(BOOT_REPS, len(d)))].mean(axis=1)
        out[f"d_{name}"] = float(d.mean()); out[f"d_{name}_ci"] = [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]
    out["pick_agreement"] = float((a.pick == b.pick).mean())
    out["edge_corr"] = float(np.corrcoef(a.edge, b.edge)[0, 1])
    return out


def keep_rule(m: dict, ref: dict) -> dict:
    seasons_pos = sum(1 for v in m["slope_by_season"].values() if v["slope"] > 0)
    checks = {
        "1_edge_slope_positive_p_lt_0.05": m["slope"] > 0 and m["slope_p"] < 0.05,
        "2_brier_below_ridge8_v1": m["brier_over"] < ref["brier_over"],
        "3a_ece_below_ridge8_v1": m["ece"] < ref["ece"],
        "3b_hi_conf_gap_below_ridge8_v1": m["hi_conf_gap_ge56"] < ref["hi_conf_gap_ge56"] if m["hi_conf_n"] else False,
        "4a_slope_positive_in_5_of_8_seasons": seasons_pos >= 5,
        "4b_slope_positive_leave_one_season_out": all(v > 0 for v in m["slope_loso"].values()),
        "5_slope_positive_over_and_under": m["slope_by_side"]["OVER"]["slope"] > 0 and m["slope_by_side"]["UNDER"]["slope"] > 0,
    }
    return {"checks": checks, "seasons_with_positive_slope": seasons_pos, "advance": all(checks.values())}


def evaluate_all(pred_dir: Path, ref_csv: Path, out_dir: Path, candidate_ids: list[str], dev_tag: str = "dev_2015_2022",
                 rep_tag: str = "replication_2010_2014", ref_rep_csv: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = extended_metrics(ref_csv)
    analyze(ref_csv, out_dir / "ridge8_v1", f"ridge8_v1_{dev_tag}")
    results = {"ridge8_v1": {"metrics": ref}}
    for cid in candidate_ids:
        f = pred_dir / f"{cid}_predictions_{dev_tag}.csv"
        m = extended_metrics(f)
        analyze(f, out_dir / cid, f"{cid}_{dev_tag}")
        results[cid] = {"metrics": m, "paired_vs_ridge8_v1": paired_vs_reference(f, ref_csv), "keep_rule": keep_rule(m, ref)}
        rf = pred_dir / f"{cid}_predictions_{rep_tag}.csv"
        if rf.exists():
            rm = extended_metrics(rf)
            results[cid]["replication"] = {k: rm[k] for k in ("games_evaluated", "brier_over", "ece", "slope", "slope_p", "win_pct", "total_mae", "market_mae")}
    if ref_rep_csv and ref_rep_csv.exists():
        rm = extended_metrics(ref_rep_csv)
        results["ridge8_v1"]["replication"] = {k: rm[k] for k in ("games_evaluated", "brier_over", "ece", "slope", "slope_p", "win_pct", "total_mae", "market_mae")}
    (out_dir / "phase3a_results.json").write_text(json.dumps(results, indent=1, default=float))
    return results
