"""Phase 3B evaluation (spec sections 6-7). Pure functions of the OOS prediction files.

Residual layer: pick = sign(r), edge = |r| points, frozen Phase 2A buckets.
Probability layer: calibrated P(over); pick = P > 0.5.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from src.nfl.backtest.phase3a_eval import PBINS, logit_slope
from src.nfl.backtest.totals_harness import BUCKETS
from src.nfl.models.residual import phase3b_config as C

WIN_PNL = 100 / 110
EPS = 1e-6
RIDGE8_FROZEN = {"brier": 0.2550, "log_loss": 0.7038, "ece_pick": 0.058}


def dev_slice(f: pd.DataFrame, dev=C.DEV) -> pd.DataFrame:
    return f[(f.game_type == "REG") & (f.season >= dev[0]) & (f.season <= dev[1]) & f.total_points.notna()
             & f.total_line.notna() & f.r.notna()].copy()


def _ll(p, y):
    p = np.clip(p, EPS, 1 - EPS)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _pearson(x, y):
    if len(x) < 3 or np.std(x) == 0:
        return float("nan"), float("nan")
    r, p = stats.pearsonr(x, y)
    return float(r), float(p)


def _boot_ci(fn, n, rng, reps):
    vals = []
    for _ in range(reps):
        idx = rng.integers(0, n, n)
        v = fn(idx)
        if np.isfinite(v):
            vals.append(v)
    return [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))] if vals else [float("nan")] * 2


def _bets(f: pd.DataFrame) -> pd.DataFrame:
    b = f[f.r != 0].copy()
    b["pick"] = np.where(b.r > 0, "OVER", "UNDER")
    b["push"] = (b.residual == 0).astype(int)
    b["win"] = (((b.r > 0) & (b.residual > 0)) | ((b.r < 0) & (b.residual < 0))).astype(int)
    b["pnl"] = np.where(b.push == 1, 0.0, np.where(b.win == 1, WIN_PNL, -1.0))
    b["abs_r"] = b.r.abs()
    return b


def _slope(dec):
    if len(dec) < 30 or dec.abs_r.std() == 0:
        return float("nan"), float("nan"), float("nan")
    return logit_slope(dec.abs_r.to_numpy(), dec.win.to_numpy())


def residual_layer(f: pd.DataFrame, rng: np.random.Generator) -> dict:
    r, res = f.r.to_numpy(float), f.residual.to_numpy(float)
    out = {"n_games": int(len(f)), "residual_mae": float(np.abs(res - r).mean()), "market_mae": float(np.abs(res).mean()),
           "residual_rmse": float(np.sqrt(((res - r) ** 2).mean())), "market_rmse": float(np.sqrt((res ** 2).mean())),
           "oos_r2": float(1 - ((res - r) ** 2).sum() / (res ** 2).sum()), "signed_bias": float((res - r).mean()),
           "mean_r": float(r.mean()), "sd_r": float(r.std()), "mean_abs_r": float(np.abs(r).mean())}
    if np.std(r) == 0:
        out["degenerate"] = True
        return out
    pr, pp = _pearson(r, res); sr, sp = stats.spearmanr(r, res)
    ols = stats.linregress(r, res)
    out.update({"pearson": pr, "pearson_p": pp, "spearman": float(sr), "spearman_p": float(sp),
                "pearson_ci": _boot_ci(lambda i: _pearson(r[i], res[i])[0], len(r), rng, C.BOOT_REPS),
                "ols_slope": float(ols.slope), "ols_slope_se": float(ols.stderr), "ols_p": float(ols.pvalue),
                "ols_ci": [float(ols.slope - 1.96 * ols.stderr), float(ols.slope + 1.96 * ols.stderr)]})
    b = _bets(f); dec = b[b.push == 0]
    s, se, p = _slope(dec)
    x, y = dec.abs_r.to_numpy(), dec.win.to_numpy()
    boot = _boot_ci(lambda i: logit_slope(x[i], y[i])[0], len(x), rng, C.BOOT_REPS_SLOPE)
    out.update({"n_bets": int(len(b)), "n_decided": int(len(dec)), "win_pct": float(dec.win.mean()),
                "directional_accuracy": float(dec.win.mean()), "roi_110": float(b.pnl.mean()),
                "over_share": float((b.pick == "OVER").mean()),
                "win_slope": s, "win_slope_se": se, "win_slope_p": p, "win_slope_ci_wald": [s - 1.96 * se, s + 1.96 * se],
                "win_slope_ci_boot": boot})
    side = {}
    for sd in ("OVER", "UNDER"):
        g = dec[dec.pick == sd]; bs, bse, bp = _slope(g)
        gp = _pearson(g.r.to_numpy(), g.residual.to_numpy())[0] if len(g) > 2 else float("nan")
        side[sd] = {"n": int(len(g)), "win_pct": float(g.win.mean()) if len(g) else float("nan"),
                    "roi_110": float(b[b.pick == sd].pnl.mean()) if len(g) else float("nan"),
                    "win_slope": bs, "win_slope_p": bp, "pearson_within_side": gp}
    out["by_side"] = side
    seas = {}
    for sn, g in f.groupby("season"):
        gb = _bets(g); gd = gb[gb.push == 0]; s_, _, p_ = _slope(gd)
        seas[int(sn)] = {"n": int(len(g)), "pearson": _pearson(g.r.to_numpy(), g.residual.to_numpy())[0],
                         "residual_mae": float((g.residual - g.r).abs().mean()), "market_mae": float(g.residual.abs().mean()),
                         "win_pct": float(gd.win.mean()) if len(gd) else float("nan"),
                         "roi_110": float(gb.pnl.mean()) if len(gb) else float("nan"), "win_slope": s_, "win_slope_p": p_,
                         "over_share": float((gb.pick == "OVER").mean()) if len(gb) else float("nan")}
    out["by_season"] = seas
    loso = {}
    for sn in sorted(f.season.unique()):
        g = f[f.season != sn]; gd = _bets(g); gd = gd[gd.push == 0]
        loso[int(sn)] = {"pearson": _pearson(g.r.to_numpy(), g.residual.to_numpy())[0], "win_slope": _slope(gd)[0]}
    out["loso"] = loso
    rows = []
    for lo, hi, name in BUCKETS:
        for sd in ("ALL", "OVER", "UNDER"):
            g = b[(b.abs_r >= lo) & (b.abs_r < hi)]
            if sd != "ALL":
                g = g[g.pick == sd]
            gd = g[g.push == 0]
            rows.append({"bucket": name, "side": sd, "n": int(len(g)), "win_pct": float(gd.win.mean()) if len(gd) else float("nan"),
                         "roi_110": float(g.pnl.mean()) if len(g) else float("nan")})
    for thr in (1.0, 2.0, 3.0):
        g = b[b.abs_r >= thr]; gd = g[g.push == 0]
        rows.append({"bucket": f">= {thr:.1f}", "side": "ALL", "n": int(len(g)),
                     "win_pct": float(gd.win.mean()) if len(gd) else float("nan"), "roi_110": float(g.pnl.mean()) if len(g) else float("nan")})
    out["buckets"] = rows
    return out


def _ece_tables(p, y):
    pick_over = p > 0.5
    p_pick = np.where(pick_over, p, 1 - p); win = np.where(pick_over, y, 1 - y)
    d = pd.DataFrame({"p_pick": p_pick, "win": win})
    d["pbin"] = pd.cut(d.p_pick, PBINS, right=False)
    t = d.groupby("pbin", observed=True).agg(n=("win", "size"), stated=("p_pick", "mean"), realized=("win", "mean")).reset_index()
    t["gap"] = t.realized - t.stated
    ece_pick = float((t.n / t.n.sum() * t.gap.abs()).sum()) if len(t) else float("nan")
    d2 = pd.DataFrame({"p": p, "y": y}); d2["bin"] = pd.cut(d2.p, np.linspace(0, 1, 11), include_lowest=True)
    t2 = d2.groupby("bin", observed=True).agg(n=("y", "size"), stated=("p", "mean"), realized=("y", "mean")).reset_index()
    ece10 = float((t2.n / t2.n.sum() * (t2.realized - t2.stated).abs()).sum())
    t["pbin"] = t.pbin.astype(str); t2["bin"] = t2.bin.astype(str)
    return ece_pick, ece10, t, t2, p_pick, win


def prob_layer(f: pd.DataFrame, col: str, rng: np.random.Generator, ref: pd.DataFrame | None = None,
               mkt: pd.DataFrame | None = None) -> dict:
    d = f[f.over.notna()]
    p, y = d[col].to_numpy(float), d.over.to_numpy(float)
    ece_pick, ece10, t, t2, p_pick, win = _ece_tables(p, y)
    out = {"n": int(len(d)), "brier": float(((p - y) ** 2).mean()), "log_loss": float(_ll(p, y).mean()),
           "ece_pick": ece_pick, "ece10": ece10, "mean_p": float(p.mean()), "sd_p": float(p.std()),
           "max_p_pick": float(p_pick.max()), "share_p_pick_ge_0.53": float((p_pick >= 0.53).mean()),
           "calibration_pick": t.to_dict(orient="records"), "calibration_over10": t2.to_dict(orient="records"),
           "sign_disagreements": int(((p > 0.5) != (d.r.to_numpy() > 0)).sum()) if d.r.std() > 0 else 0}
    pe = p_pick - 0.5
    if pe.std() > 0:
        s, se, pv = logit_slope(pe, win)
        out.update({"prob_win_slope": s, "prob_win_slope_p": pv, "prob_win_slope_ci": [s - 1.96 * se, s + 1.96 * se],
                    "prob_win_pct": float(win.mean())})
    for name, other, ocol in (("vs_ridge8_v1", ref, "p_raw"), ("vs_market", mkt, "p_mkt")):
        if other is None:
            continue
        a = d.set_index("game_id"); o = other[other.over.notna()].set_index("game_id")
        common = a.index.intersection(o.index); a, o = a.loc[common], o.loc[common]
        db = ((a[col] - a.over) ** 2 - (o[ocol] - o.over) ** 2).to_numpy()
        dl = (_ll(a[col].to_numpy(), a.over.to_numpy()) - _ll(o[ocol].to_numpy(), o.over.to_numpy()))
        res = {"n_common": int(len(common))}
        for k, v in (("d_brier", db), ("d_log_loss", dl)):
            bs = v[rng.integers(0, len(v), size=(C.BOOT_REPS, len(v)))].mean(axis=1)
            res[k] = float(v.mean()); res[k + "_ci"] = [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]
        out[name] = res
    return out


def keep_rule(rl: dict, pl: dict, ridge8_ece10: float, mkt_brier: float) -> dict:
    if rl.get("degenerate"):
        return {"checks": {}, "advance": False, "note": "degenerate (no predicted residual)"}
    seasons_pos = sum(1 for v in rl["by_season"].values() if v["pearson"] > 0)
    checks = {
        "1_residual_signal_pearson_p_lt_0.05_and_r2_gt_0": rl["pearson"] > 0 and rl["pearson_p"] < 0.05 and rl["oos_r2"] > 0,
        "2_edge_vs_win_slope_p_lt_0.05": rl["win_slope"] > 0 and rl["win_slope_p"] < 0.05,
        "3_prob_quality_beats_ridge8_and_market": pl["brier"] < RIDGE8_FROZEN["brier"] and pl["log_loss"] < RIDGE8_FROZEN["log_loss"]
        and pl["brier"] < mkt_brier,
        "4_calibration_ece_half_ridge8_and_ece10_below": pl["ece_pick"] <= RIDGE8_FROZEN["ece_pick"] / 2 and pl["ece10"] < ridge8_ece10,
        "5_both_sides_and_5_of_8_seasons": rl["by_side"]["OVER"]["win_slope"] > 0 and rl["by_side"]["UNDER"]["win_slope"] > 0
        and seasons_pos >= 5,
        "6_loso_positive": all(v["pearson"] > 0 and v["win_slope"] > 0 for v in rl["loso"].values()),
    }
    return {"checks": checks, "seasons_pearson_positive": seasons_pos, "advance": all(checks.values())}


def choose_calibrator(pls: dict) -> str:
    best = min(v["log_loss"] for v in pls.values())
    for m in C.CAL_SIMPLICITY_ORDER:
        if pls[m]["log_loss"] <= best + C.CAL_LOGLOSS_TOLERANCE:
            return m
    return "logistic"


def evaluate_dir(pred_dir: Path, model_ids: list[str], out_path: Path) -> dict:
    rng = np.random.default_rng(C.SEED)
    frames = {m: dev_slice(pd.read_csv(pred_dir / f"{m}_oos.csv")) for m in model_ids}
    ref = frames["ridge8_v1"]; mkt = frames["m0_market"]
    _, ridge8_ece10, *_ = _ece_tables(ref[ref.over.notna()].p_raw.to_numpy(), ref[ref.over.notna()].over.to_numpy())
    mkt_brier = float(((mkt[mkt.over.notna()].p_mkt - mkt[mkt.over.notna()].over) ** 2).mean())
    results = {"_reference": {"ridge8_v1_ece10": ridge8_ece10, "market_brier": mkt_brier, "ridge8_frozen": RIDGE8_FROZEN}}
    for m in model_ids:
        f = frames[m]
        rl = residual_layer(f, rng)
        pls = {c: prob_layer(f, f"p_{c}", rng, ref, mkt) for c in ("raw", "logistic", "shrink", "isotonic")}
        pls["market"] = prob_layer(f, "p_mkt", rng, ref, mkt)
        chosen = choose_calibrator({k: pls[k] for k in ("logistic", "shrink", "isotonic")})
        results[m] = {"residual": rl, "prob": pls, "calibrator_chosen": chosen,
                      "keep_rule": keep_rule(rl, pls[chosen], ridge8_ece10, mkt_brier),
                      "keep_rule_primary_logistic": keep_rule(rl, pls["logistic"], ridge8_ece10, mkt_brier)}
    out_path.write_text(json.dumps(results, indent=1, default=float))
    return results
