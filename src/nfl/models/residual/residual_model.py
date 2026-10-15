"""Phase 3B market-residual model (spec: docs/phase3b/PHASE_3B_SPEC.md sections 1-3).

Target: residual = total_points - total_line. Prediction: total = line + predicted residual.

Walk-forward, expanding window: for test season S the model is fit only on games with
season < S. Ridge alpha (and, for the conservative model, which feature groups to include)
is chosen on inner folds made of seasons before S, so the test season never influences a
choice. Standardization is fit on the training fold only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from src.nfl.models.residual import phase3b_config as C


def add_residual_target(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["residual"] = out["total_points"] - out["total_line"]
    return out


def _fit_predict(train: pd.DataFrame, test: pd.DataFrame, features: list[str], alpha: float):
    X = train[features].to_numpy(float); y = train["residual"].to_numpy(float)
    sc = StandardScaler().fit(X)
    m = Ridge(alpha=alpha).fit(sc.transform(X), y)
    fitted = m.predict(sc.transform(X))
    pred = m.predict(sc.transform(test[features].to_numpy(float))) if len(test) else np.array([])
    sigma = float(np.std(y - fitted, ddof=1))
    return pred, sigma, m, sc


def _usable(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    return df[df["total_points"].notna() & df["total_line"].notna()].dropna(subset=features)


def _train_ok(df: pd.DataFrame, season: int, features: list[str], first_train_season: int) -> pd.DataFrame:
    return _usable(df[(df["season"] < season) & (df["season"] >= first_train_season)], features)


def inner_folds(df: pd.DataFrame, season: int, features: list[str], first_train_season: int) -> list[int]:
    folds = []
    for s in sorted(df["season"].unique()):
        if s >= season:
            break
        if len(_train_ok(df, s, features, first_train_season)) >= C.MIN_TRAIN_GAMES:
            folds.append(int(s))
    return folds[-C.INNER_FOLDS:]


def inner_mse(df: pd.DataFrame, season: int, features: list[str], first_train_season: int) -> dict[float, float]:
    """Pooled inner-fold residual MSE per alpha, using only seasons before `season`."""
    folds = inner_folds(df, season, features, first_train_season)
    if not folds:
        return {}
    sse = {a: 0.0 for a in C.ALPHA_GRID}; n = 0
    for s in folds:
        tr = _train_ok(df, s, features, first_train_season)
        te = _usable(df[df["season"] == s], features)
        if len(te) == 0:
            continue
        n += len(te)
        for a in C.ALPHA_GRID:
            p, *_ = _fit_predict(tr, te, features, a)
            sse[a] += float(((te["residual"].to_numpy() - p) ** 2).sum())
    return {a: v / n for a, v in sse.items()} if n else {}


def walk_forward_residual(df: pd.DataFrame, features: list[str], *, select_groups: dict[str, list[str]] | None = None,
                          first_train_season: int = C.FIRST_SEASON, last_season: int | None = None) -> pd.DataFrame:
    """Return df with r_hat (predicted residual) and r_sigma for every season that has enough
    prior training data. If `select_groups` is given, `features` is the base set and each group is
    included for season S only if it lowers the inner-fold MSE (nested, prior seasons only)."""
    df = add_residual_target(df).sort_values(["season", "week", "gameday", "game_id"]).reset_index(drop=True)
    if last_season is not None:
        df = df[df["season"] <= last_season].reset_index(drop=True)
    r_hat = pd.Series(np.nan, index=df.index); r_sig = pd.Series(np.nan, index=df.index)
    log = {}
    all_feats = features + [f for g in (select_groups or {}).values() for f in g]
    for season in sorted(df["season"].unique()):
        feats = list(features)
        chosen = []
        if select_groups:
            base = inner_mse(df, season, features, first_train_season)
            base_best = min(base.values()) if base else None
            for gname, gfeats in select_groups.items():
                cand = inner_mse(df, season, features + gfeats, first_train_season)
                if base_best is not None and cand and min(cand.values()) < base_best:
                    chosen.append(gname); feats += gfeats
        mse = inner_mse(df, season, feats, first_train_season)
        alpha = min(mse, key=mse.get) if mse else C.ALPHA_DEFAULT
        train = _train_ok(df, season, feats, first_train_season)
        if len(train) < C.MIN_TRAIN_GAMES:
            continue
        test = df[(df["season"] == season)].dropna(subset=feats)
        test = test[test["total_line"].notna()]
        if len(test) == 0:
            continue
        pred, sigma, m, sc = _fit_predict(train, test, feats, alpha)
        r_hat.loc[test.index] = pred; r_sig.loc[test.index] = sigma
        log[int(season)] = {"alpha": alpha, "n_train": int(len(train)), "groups": chosen, "n_features": len(feats),
                            "coef_std": dict(zip(feats, map(float, m.coef_))), "intercept": float(m.intercept_)}
    out = df.copy(); out["r_hat"] = r_hat; out["r_sigma"] = r_sig
    out.attrs["fit_log"] = log
    out.attrs["all_features"] = all_feats
    return out
