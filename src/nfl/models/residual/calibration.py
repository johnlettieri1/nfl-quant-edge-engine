"""Phase 3B walk-forward probability calibration (spec section 4).

Every calibrator for test season S is trained only on out-of-sample predictions from seasons
before S. No observation calibrates itself; no calibrator sees its own test season.

Input frame columns: season, r (predicted residual / point edge), r_sigma, over (1/0, NaN on push).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from scipy.special import expit
from scipy.stats import norm
from sklearn.isotonic import IsotonicRegression

from src.nfl.models.residual import phase3b_config as C

EPS = 1e-6


def _ll(p, y):
    p = np.clip(p, EPS, 1 - EPS)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def fit_logistic(r: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """MLE of logit P = a + b r by IRLS (unregularized; 2 parameters, n >= 500)."""
    X = np.column_stack([np.ones(len(r)), r]); beta = np.zeros(2)
    for _ in range(100):
        p = expit(X @ beta); W = p * (1 - p)
        step = np.linalg.solve((X * W[:, None]).T @ X + 1e-9 * np.eye(2), X.T @ (y - p)); beta += step
        if np.abs(step).max() < 1e-10:
            break
    return float(beta[0]), float(beta[1])


def fit_shrink(praw: np.ndarray, y: np.ndarray) -> float:
    res = minimize_scalar(lambda lam: _ll(0.5 + lam * (praw - 0.5), y), bounds=(0.0, 1.0), method="bounded")
    return float(res.x)


def raw_prob(r, sigma):
    return norm.cdf(np.asarray(r, float) / np.asarray(sigma, float))


def walk_forward_calibrate(frame: pd.DataFrame) -> pd.DataFrame:
    """Adds p_raw, p_logistic, p_shrink, p_isotonic and a per-season parameter log (attrs)."""
    f = frame.copy()
    f["p_raw"] = raw_prob(f["r"], f["r_sigma"])
    for c in ("p_logistic", "p_shrink", "p_isotonic"):
        f[c] = np.nan
    log = {}
    for s in sorted(f["season"].unique()):
        tr = f[(f["season"] < s) & f["over"].notna() & f["r"].notna()]
        te = f.index[(f["season"] == s) & f["r"].notna()]
        if len(te) == 0:
            continue
        if len(tr) < C.CAL_MIN_ROWS:
            f.loc[te, ["p_logistic", "p_shrink", "p_isotonic"]] = 0.5
            log[int(s)] = {"n_cal": int(len(tr)), "fallback": True}
            continue
        r, y, pr = tr["r"].to_numpy(), tr["over"].to_numpy(), tr["p_raw"].to_numpy()
        a, b = fit_logistic(r, y)
        lam = fit_shrink(pr, y)
        iso = IsotonicRegression(y_min=C.ISOTONIC_CLIP[0], y_max=C.ISOTONIC_CLIP[1], increasing=True,
                                 out_of_bounds="clip").fit(r, y)
        rt = f.loc[te, "r"].to_numpy()
        f.loc[te, "p_logistic"] = expit(a + b * rt)
        f.loc[te, "p_shrink"] = 0.5 + lam * (f.loc[te, "p_raw"].to_numpy() - 0.5)
        f.loc[te, "p_isotonic"] = iso.predict(rt)
        log[int(s)] = {"n_cal": int(len(tr)), "logistic_a": a, "logistic_b": b, "shrink_lambda": lam,
                       "cal_seasons": [int(tr.season.min()), int(tr.season.max())]}
    f.attrs["cal_log"] = log
    return f


def no_vig_over(over_odds: pd.Series, under_odds: pd.Series) -> pd.Series:
    """No-vig P(over) from American closing odds; 0.5 where odds are missing."""
    def imp(o):
        o = pd.to_numeric(o, errors="coerce")
        return np.where(o > 0, 100 / (o + 100), np.where(o < 0, -o / (-o + 100), np.nan))
    io, iu = imp(over_odds), imp(under_odds)
    p = io / (io + iu)
    return pd.Series(np.where(np.isfinite(p), p, 0.5), index=over_odds.index)
