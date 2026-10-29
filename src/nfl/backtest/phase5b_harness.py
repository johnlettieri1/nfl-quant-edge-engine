"""Phase 5B harness: runs the pre-registered spread residual candidates and scores them against the
pass bar in docs/phase5/PHASE_5B_PREREGISTRATION.md (frozen Oct 29 2026).

Pass bar (both required, dev 2015-2022 REG, out of sample):
  Bar 1  paired bootstrap (10,000 reps, seed 20261029) of per-game |margin-(line+r_hat)| - |margin-line|;
         the interval must sit entirely below zero.
  Bar 2  Wilson interval on the cover rate of sign(r_hat) picks (non-push, r_hat != 0); lower bound > 50%.
Decision intervals use 1 - 0.05/8 (eight candidates); 95% intervals are reported alongside.
Breakeven 52.4% at -110 is reported, not part of the bar. Holdout 2023+ never enters.

Run: python -m src.nfl.backtest.phase5b_harness  (writes docs/phase5/results/)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from src.nfl.backtest.totals_harness import DEV, HOLDOUT_START, REPLICATION
from src.nfl.models.residual.calibration import no_vig_over
from src.nfl.models.spread.residual_spread import CANDIDATES, build_spread_frame, walk_forward_spread_residual

SEED = 20261029
BOOT_REPS = 10_000
N_CANDIDATES = 8
ALPHA_FAMILY = 0.05 / N_CANDIDATES
BREAKEVEN = 110 / 210
DESCRIPTIVE_CUTS = (1.0, 2.0)
OUT_COLS = ["game_id", "season", "week", "game_type", "gameday", "away_team", "home_team", "home_score", "away_score",
            "margin", "spread_line", "home_spread_odds", "away_spread_odds", "spread_residual", "r_hat", "r_sigma",
            "model_version"]


# ----------------------------------------------------------------------------- statistics

def wilson(k: int, n: int, conf: float) -> tuple[float, float, float]:
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    z = norm.ppf(1 - (1 - conf) / 2); p = k / n
    den = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / den
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return float(p), float(cen - half), float(cen + half)


def paired_bootstrap(diff: np.ndarray, conf: float, reps: int = BOOT_REPS, seed: int = SEED) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(diff), size=(reps, len(diff)))
    means = diff[idx].mean(axis=1)
    lo, hi = np.quantile(means, [(1 - conf) / 2, 1 - (1 - conf) / 2])
    return float(diff.mean()), float(lo), float(hi)


def _slice(wf: pd.DataFrame, lo: int, hi: int) -> pd.DataFrame:
    return wf[(wf.game_type == "REG") & wf.spread_line.notna() & wf.r_hat.notna() & wf.margin.notna()
              & (wf.season >= lo) & (wf.season <= hi)].copy()


def score(ev: pd.DataFrame) -> dict:
    """All pass-bar and descriptive statistics for one evaluation slice."""
    margin = ev["margin"].to_numpy(float); line = ev["spread_line"].to_numpy(float); r = ev["r_hat"].to_numpy(float)
    err_model = np.abs(margin - (line + r)); err_mkt = np.abs(margin - line)
    diff = err_model - err_mkt
    out = {"n": int(len(ev)), "mae_market": float(err_mkt.mean()), "mae_model": float(err_model.mean())}
    for tag, conf in (("95", 0.95), ("family", 1 - ALPHA_FAMILY)):
        m, lo, hi = paired_bootstrap(diff, conf)
        out[f"bar1_mean_diff"] = m; out[f"bar1_ci{tag}"] = [lo, hi]
    # cover rate: non-push, r_hat != 0
    res = ev["spread_residual"].to_numpy(float)
    pick = (res != 0) & (r != 0)
    win = ((r > 0) & (res > 0)) | ((r < 0) & (res < 0))
    k, n = int(win[pick].sum()), int(pick.sum())
    out["bar2_n"] = n; out["bar2_wins"] = k
    for tag, conf in (("95", 0.95), ("family", 1 - ALPHA_FAMILY)):
        p, lo, hi = wilson(k, n, conf)
        out["cover_rate"] = p; out[f"bar2_ci{tag}"] = [lo, hi]
    out["pushes"] = int((res == 0).sum()); out["no_pick"] = int((r == 0).sum())
    out["home_pick_share"] = float((r > 0)[pick].mean()) if n else float("nan")
    out["bar1_pass"] = bool(out["bar1_cifamily"][1] < 0)
    out["bar2_pass"] = bool(out["bar2_cifamily"][0] > 0.5)
    out["PASS"] = out["bar1_pass"] and out["bar2_pass"]
    out["breakeven_point_estimate_clears"] = bool(out["cover_rate"] > BREAKEVEN) if n else False
    out["breakeven_lower95_clears"] = bool(out["bar2_ci95"][0] > BREAKEVEN) if n else False
    # descriptive cuts (reported only)
    for cut in DESCRIPTIVE_CUTS:
        sel = pick & (np.abs(r) >= cut)
        kc, nc = int(win[sel].sum()), int(sel.sum())
        p, lo, hi = wilson(kc, nc, 0.95)
        out[f"cut_{cut:.1f}"] = {"n": nc, "wins": kc, "cover_rate": p, "ci95": [lo, hi]}
    # probability quality where spread prices exist
    if ev["home_spread_odds"].notna().any():
        has = ev["home_spread_odds"].notna() & ev["away_spread_odds"].notna() & (res != 0)
        e = ev[has]
        p_mkt = no_vig_over(e["home_spread_odds"], e["away_spread_odds"]).to_numpy(float)
        z = (e["r_hat"].to_numpy(float)) / e["r_sigma"].to_numpy(float)
        p_mod = norm.cdf(z)                      # P(home covers) = P(residual > 0)
        y = (e["spread_residual"].to_numpy(float) > 0).astype(float)
        eps = 1e-6
        def ll(p): return float(-np.mean(y * np.log(np.clip(p, eps, 1 - eps)) + (1 - y) * np.log(np.clip(1 - p, eps, 1 - eps))))
        out["prob"] = {"n": int(len(e)), "logloss_model": ll(p_mod), "logloss_market": ll(p_mkt),
                       "brier_model": float(np.mean((p_mod - y) ** 2)), "brier_market": float(np.mean((p_mkt - y) ** 2))}
    # per season
    per = {}
    for s, g in ev.groupby("season"):
        gm = g["margin"].to_numpy(float); gl = g["spread_line"].to_numpy(float); gr = g["r_hat"].to_numpy(float)
        gres = g["spread_residual"].to_numpy(float)
        gp = (gres != 0) & (gr != 0); gw = ((gr > 0) & (gres > 0)) | ((gr < 0) & (gres < 0))
        per[int(s)] = {"n": int(len(g)), "mae_model": float(np.abs(gm - gl - gr).mean()), "mae_market": float(np.abs(gm - gl).mean()),
                       "cover_rate": float(gw[gp].mean()) if gp.sum() else float("nan"), "picks": int(gp.sum())}
    out["per_season"] = per
    out["abs_r_hat_mean"] = float(np.abs(r).mean())
    return out


def calibration_table(ev: pd.DataFrame, bins: int = 5) -> list[dict]:
    e = ev[ev.spread_residual != 0].copy()
    e["p"] = norm.cdf(e["r_hat"] / e["r_sigma"]); e["y"] = (e["spread_residual"] > 0).astype(float)
    e["bin"] = pd.qcut(e["p"], bins, labels=False, duplicates="drop")
    return [{"bin": int(b), "n": int(len(g)), "p_mean": float(g.p.mean()), "home_cover_rate": float(g.y.mean())}
            for b, g in e.groupby("bin")]


# ----------------------------------------------------------------------------- run

def run_phase5b(out_dir: Path, raw_dir: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = build_spread_frame(raw_dir, HOLDOUT_START)
    assert frame.season.max() < HOLDOUT_START
    results, logs, dropped = {}, {}, {}
    # baseline on the same games as the widest candidate (s3_margin4 window)
    for cid, (feats, first) in CANDIDATES.items():
        missing = [f for f in feats if f not in frame.columns]
        if missing:
            dropped[cid] = missing
            continue
        wf = walk_forward_spread_residual(frame, feats, first_train_season=first)
        wf["model_version"] = cid
        logs[cid] = {"features": feats, "first_train_season": first, "fit_log": wf.attrs["fit_log"]}
        res = {}
        for lo, hi, tag in ((DEV[0], DEV[1], "dev"), (REPLICATION[0], REPLICATION[1], "replication")):
            ev = _slice(wf, lo, hi)
            if len(ev) == 0:            # 2013+ candidates have no replication-window predictions
                res[tag] = {"n": 0}
                continue
            assert ev.season.max() < HOLDOUT_START
            ev[OUT_COLS].to_csv(out_dir / f"{cid}_oos_{tag}_{lo}_{hi}.csv", index=False)
            res[tag] = score(ev)
            if tag == "dev":
                res["calibration_dev"] = calibration_table(ev)
                # market baseline on exactly these games (r_hat = 0): bar statistics are trivially zero/no-pick
                m0 = ev.copy(); m0["r_hat"] = 0.0
                res["market_same_games"] = {"n": int(len(m0)), "mae_market": float(np.abs(m0.margin - m0.spread_line).mean()),
                                            "home_cover_rate": float(((m0.spread_residual > 0)[m0.spread_residual != 0]).mean())}
        results[cid] = res
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(), "seed": SEED, "boot_reps": BOOT_REPS,
            "family_alpha": ALPHA_FAMILY, "candidates": list(results), "dropped": dropped,
            "dev": list(DEV), "replication": list(REPLICATION), "holdout_untouched": [HOLDOUT_START, 2025],
            "max_season_in_frame": int(frame.season.max()), "n_frame_rows": int(len(frame)),
            "python": sys.version.split()[0], "pandas": pd.__version__, "numpy": np.__version__}
    (out_dir / "phase5b_results.json").write_text(json.dumps(results, indent=1, default=float))
    (out_dir / "phase5b_fit_logs.json").write_text(json.dumps(logs, indent=1, default=float))
    (out_dir / "phase5b_meta.json").write_text(json.dumps(meta, indent=2))
    # summary table
    rows = []
    for cid, r in results.items():
        d = r["dev"]
        rows.append({"candidate": cid, "n": d["n"], "mae_market": round(d["mae_market"], 3), "mae_model": round(d["mae_model"], 3),
                     "bar1_mean_diff": round(d["bar1_mean_diff"], 3),
                     "bar1_lo95": round(d["bar1_ci95"][0], 3), "bar1_hi95": round(d["bar1_ci95"][1], 3),
                     "bar1_lo_family": round(d["bar1_cifamily"][0], 3), "bar1_hi_family": round(d["bar1_cifamily"][1], 3),
                     "bar1_pass": d["bar1_pass"], "picks": d["bar2_n"], "cover_rate": round(d["cover_rate"], 4),
                     "bar2_lo95": round(d["bar2_ci95"][0], 4), "bar2_hi95": round(d["bar2_ci95"][1], 4),
                     "bar2_lo_family": round(d["bar2_cifamily"][0], 4), "bar2_hi_family": round(d["bar2_cifamily"][1], 4),
                     "bar2_pass": d["bar2_pass"], "PASS": d["PASS"],
                     "clears_52.4_point": d["breakeven_point_estimate_clears"], "clears_52.4_lower95": d["breakeven_lower95_clears"]})
    pd.DataFrame(rows).to_csv(out_dir / "phase5b_summary_dev_2015_2022.csv", index=False)
    return {"results": results, "meta": meta}


if __name__ == "__main__":
    out = run_phase5b(Path("docs/phase5/results"))
    for cid, r in out["results"].items():
        d = r["dev"]
        print(f"{cid:14s} n={d['n']} MAE mkt={d['mae_market']:.3f} model={d['mae_model']:.3f} "
              f"d={d['bar1_mean_diff']:+.3f} ci_fam={d['bar1_cifamily']} | cover={d['cover_rate']:.4f} "
              f"ci_fam={d['bar2_cifamily']} picks={d['bar2_n']} | PASS={d['PASS']}")
