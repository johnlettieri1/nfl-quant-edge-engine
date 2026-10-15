"""Phase 2B: the common harness must produce byte-identical development predictions
whether or not holdout/future seasons exist in the cache, and must never emit a
prediction for a holdout season."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.nfl.backtest.totals_harness import run_baseline
from tests.leakage._helpers import fake_elo_games, fake_pbp_for_games


def _write_cache(tmp_path, games, pbp):
    d = tmp_path; (d / "pbp").mkdir(parents=True, exist_ok=True)
    games.to_parquet(d / "schedules.parquet", index=False)
    pbp.to_parquet(d / "pbp_epa.parquet", index=False)


def _fake(n_seasons):
    g = fake_elo_games(n_seasons=n_seasons, games_per_season=80, seed=3)
    g["season"] = g["season"] - 2015 + 2012          # seasons 2012.. so that dev 2015-2022 is populated
    g["game_id"] = g["season"].astype(str) + "_" + g["game_id"]
    g["total_line"] = 44.0; g["over_odds"] = -110; g["under_odds"] = -110
    g["roof"] = "outdoors"
    pbp = fake_pbp_for_games(g, seed=3)
    return g, pbp


def test_dev_predictions_unchanged_by_holdout_and_future_seasons(tmp_path):
    short_games, short_pbp = _fake(9)                  # 2012-2020
    a = tmp_path / "a"; _write_cache(a, short_games, short_pbp)
    long_games, long_pbp = _fake(15)                   # 2012-2026, includes holdout + future
    future = long_games.season >= 2021
    long_games.loc[future, ["home_score", "away_score"]] = 999   # corrupt everything after the short window
    b = tmp_path / "b"; _write_cache(b, long_games, long_pbp)
    run_baseline(tmp_path / "out_a", raw_dir=a, dev=(2015, 2020), holdout_start=2021)
    run_baseline(tmp_path / "out_b", raw_dir=b, dev=(2015, 2020), holdout_start=2021)
    pa = pd.read_csv(tmp_path / "out_a" / "baseline_predictions_dev_2015_2020.csv").set_index("game_id").sort_index()
    pb = pd.read_csv(tmp_path / "out_b" / "baseline_predictions_dev_2015_2020.csv").set_index("game_id").sort_index()
    assert list(pa.index) == list(pb.index)
    np.testing.assert_allclose(pa.total_mean_pred.values, pb.total_mean_pred.values, atol=1e-9)
    assert pb.season.max() <= 2020, "a holdout-season prediction leaked into the development file"
