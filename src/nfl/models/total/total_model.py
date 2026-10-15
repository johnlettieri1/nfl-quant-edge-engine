"""Walk-forward game-total (points) model.

Predicts total points as Normal(mean, sigma) so over/under probability
against any market total line can be derived from the CDF. Leak-free:
features are trailing scoring averages (see rolling_scoring.py, itself
leak-free), and the regression mapping those features -> total points is
fit only on strictly-prior-season data, expanding window.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.linear_model import Ridge

from src.nfl.features.rolling_scoring import add_trailing_scoring_features

# Phase 2B: version tag recorded in every snapshot and ledger row. Bump when the
# feature set, windows, regularization, or probability mapping change.
MODEL_VERSION = "ridge8_v1"

FEATURE_COLS = [
    "home_off_trailing", "home_def_trailing", "away_off_trailing", "away_def_trailing",
    "home_epa_matchup", "away_epa_matchup", "is_dome", "qb_epa_sum",
]


def build_total_features(games: pd.DataFrame, window: int = 16) -> pd.DataFrame:
    """`games` is expected to already carry trailing EPA columns from
    features/epa_features.add_trailing_epa_features and trailing QB columns
    from features/qb_features.add_trailing_qb_features (see margin_model.py's
    equivalent note) -- box-score trailing scoring features are computed
    here since they don't need the separate play-by-play frame.

    `roof` is known before kickoff for every game (it's a venue property,
    not a forecast) -- unlike wind/temp, which nflverse only records
    post-game and are therefore unusable as a live prediction feature (see
    README's "what's not built yet": integrating a weather forecast API
    would let a live wind feature be added honestly; recorded wind cannot).
    """
    df = add_trailing_scoring_features(games, window=window)
    df["total_points"] = df["home_score"] + df["away_score"]

    df["home_epa_matchup"] = df["home_off_epa_trailing"] - df["away_def_epa_allowed_trailing"]
    df["away_epa_matchup"] = df["away_off_epa_trailing"] - df["home_def_epa_allowed_trailing"]
    df["is_dome"] = df["roof"].isin(["dome", "closed"]).astype(float)
    # Sum, not diff: for total points, what matters is how good BOTH
    # quarterbacks are (raises the combined scoring ceiling), not which
    # team's QB is relatively better (that's a margin signal, not a total one).
    df["qb_epa_sum"] = df["home_qb_epa_trailing"] + df["away_qb_epa_trailing"]
    return df


def walk_forward_total(games: pd.DataFrame, min_train_games: int = 500, window: int = 16) -> pd.DataFrame:
    df = build_total_features(games, window=window)
    df = df.sort_values(["season", "week", "gameday", "game_id"]).reset_index(drop=True)

    mean_pred = pd.Series(np.nan, index=df.index)
    sigma_pred = pd.Series(np.nan, index=df.index)

    for season in sorted(df["season"].unique()):
        train = df[(df["season"] < season) & df["total_points"].notna()].dropna(subset=FEATURE_COLS)
        test_idx = df.index[df["season"] == season]
        if len(train) < min_train_games:
            continue

        X_train = train[FEATURE_COLS].values
        model = Ridge(alpha=10.0)
        model.fit(X_train, train["total_points"].values)
        resid = train["total_points"].values - model.predict(X_train)
        sigma = float(np.std(resid, ddof=1))  # flat per-season sigma -- see models/variance.py

        test = df.loc[test_idx].dropna(subset=FEATURE_COLS)
        if len(test) == 0:
            continue
        preds = model.predict(test[FEATURE_COLS].values)

        mean_pred.loc[test.index] = preds
        sigma_pred.loc[test.index] = sigma

    out = df.copy()
    out["total_mean_pred"] = mean_pred
    out["total_sigma_pred"] = sigma_pred
    return out


def over_probability(total_mean: pd.Series, total_sigma: pd.Series, total_line: pd.Series) -> pd.Series:
    z = (total_line - total_mean) / total_sigma
    return pd.Series(1.0 - norm.cdf(z), index=total_mean.index)


def over_actual(total_points: pd.Series, total_line: pd.Series) -> pd.Series:
    diff = total_points - total_line
    return pd.Series(np.where(diff > 0, 1.0, np.where(diff < 0, 0.0, 0.5)), index=total_points.index)
