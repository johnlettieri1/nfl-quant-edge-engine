"""Leakage tests for the walk-forward XGBoost models (models/gbm.py-backed).

Not used in production (see README: backtested, didn't beat Ridge/logistic
on calibration) but checked in as a permanent backtest comparison, so it
must hold the same leak-free guarantee as everything else in this repo.
"""
from __future__ import annotations

import pandas as pd
import pytest

# Phase 2B: xgboost moved to the optional [research] extra (the GBM was tested and
# rejected). The leakage guarantee is still checked whenever it is installed.
pytest.importorskip("xgboost")

from src.nfl.models.moneyline.xgb_model import walk_forward_xgb_moneyline
from src.nfl.models.spread.xgb_model import walk_forward_xgb_margin
from src.nfl.models.total.xgb_model import walk_forward_xgb_total
from tests.leakage._helpers import fake_elo_games as _fake_elo_games


class TestXgbMoneylineLeakage:
    def test_predictions_for_early_seasons_unaffected_by_later_season_corruption(self):
        games = _fake_elo_games()
        baseline = walk_forward_xgb_moneyline(games, min_train_games=50)

        corrupted = games.copy()
        last_season = corrupted["season"].max()
        mask = corrupted["season"] == last_season
        corrupted.loc[mask, "home_score"] = 1000
        corrupted.loc[mask, "away_score"] = 0
        corrupted_preds = walk_forward_xgb_moneyline(corrupted, min_train_games=50)

        earlier_mask = games["season"] < last_season
        pd.testing.assert_series_equal(
            baseline[earlier_mask].reset_index(drop=True),
            corrupted_preds[earlier_mask].reset_index(drop=True),
            check_names=False,
        )


class TestXgbMarginLeakage:
    def test_predictions_for_early_seasons_unaffected_by_later_season_corruption(self):
        games = _fake_elo_games()
        baseline = walk_forward_xgb_margin(games, min_train_games=50)

        corrupted = games.copy()
        last_season = corrupted["season"].max()
        mask = corrupted["season"] == last_season
        corrupted.loc[mask, "home_score"] = 1000
        corrupted.loc[mask, "away_score"] = 0
        corrupted_result = walk_forward_xgb_margin(corrupted, min_train_games=50)

        earlier_mask = games["season"] < last_season
        pd.testing.assert_series_equal(
            baseline.loc[earlier_mask, "margin_mean_pred"].reset_index(drop=True),
            corrupted_result.loc[earlier_mask, "margin_mean_pred"].reset_index(drop=True),
            check_names=False,
        )


class TestXgbTotalLeakage:
    def test_predictions_for_early_seasons_unaffected_by_later_season_corruption(self):
        games = _fake_elo_games()
        baseline = walk_forward_xgb_total(games, min_train_games=50)

        corrupted = games.copy()
        last_season = corrupted["season"].max()
        mask = corrupted["season"] == last_season
        corrupted.loc[mask, "home_score"] = 1000
        corrupted.loc[mask, "away_score"] = 0
        corrupted_result = walk_forward_xgb_total(corrupted, min_train_games=50)

        earlier_mask = games["season"] < last_season
        pd.testing.assert_series_equal(
            baseline.loc[earlier_mask, "total_mean_pred"].reset_index(drop=True),
            corrupted_result.loc[earlier_mask, "total_mean_pred"].reset_index(drop=True),
            check_names=False,
        )
