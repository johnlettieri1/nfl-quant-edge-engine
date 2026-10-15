"""Phase 3A leakage tests: every new feature (EW form, pace, PROE, explosives,
expected possessions) and the candidate harness.

What each test proves:
  - future invariance: corrupting every game after a cutoff (scores and play-by-play)
    leaves every feature for games up to the cutoff bit-for-bit unchanged;
  - own-game invariance: a game's features never depend on that game's own data;
  - priors: season S priors depend only on season S-1;
  - harness: development predictions are identical whether or not holdout seasons
    exist in the cache, and no holdout prediction is ever written;
  - equivalence: the generic walk-forward reproduces the ridge8_v1 walk-forward exactly
    when standardization is off;
  - franchise normalization and reliability windows behave as specified.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.nfl.backtest.phase3a_harness import walk_forward_generic
from src.nfl.features import phase3a_config as C
from src.nfl.features.phase3a_features import (TEAM_METRICS, add_phase3a_features, aggregate_style_to_game_team,
                                               normalize_schedule_teams, season_priors)
from src.nfl.models.total.total_model import FEATURE_COLS, build_total_features, walk_forward_total
from tests.leakage._helpers import fake_elo_games, fake_pbp_for_games

P3_COLS = C.GROUP_A + C.GROUP_B + C.GROUP_C + C.GROUP_D
EW_COLS = [f"{s}_{m}_ew" for s in ("home", "away") for m in TEAM_METRICS]


def fake_games(n_seasons=4, per_season=40, start=2004, seed=0):
    g = fake_elo_games(n_seasons=n_seasons, games_per_season=per_season, seed=seed)
    g["season"] = g["season"] - 2015 + start
    g["game_id"] = g["season"].astype(str) + "_" + g["game_id"]
    g["total_line"] = 44.0; g["over_odds"] = -110; g["under_odds"] = -110
    return g


def fake_style_pbp(games: pd.DataFrame, seed: int = 0, plays_per_team: int = 24) -> pd.DataFrame:
    rng = np.random.default_rng(seed); rows = []
    for g in games.itertuples(index=False):
        for team, opp in ((g.home_team, g.away_team), (g.away_team, g.home_team)):
            gsr = 3600.0
            for i in range(plays_per_team):
                drop = rng.random() < 0.6
                gsr -= rng.uniform(5, 40)
                rows.append({"game_id": g.game_id, "play_id": float(i), "season": g.season, "week": g.week,
                             "season_type": "REG", "home_team": g.home_team, "away_team": g.away_team,
                             "posteam": team, "defteam": opp, "play": 1.0, "pass": float(drop), "rush": float(not drop),
                             "epa": rng.normal(0, 1), "success": float(rng.random() < 0.45),
                             "yards_gained": float(rng.integers(-5, 40)), "qb_dropback": float(drop),
                             "qb_kneel": 0.0, "qb_spike": 0.0, "qb_scramble": 0.0, "sack": 0.0, "pass_attempt": float(drop),
                             "down": float(rng.integers(1, 5)), "ydstogo": 10.0, "yardline_100": 50.0,
                             "qtr": float(1 + i * 4 // plays_per_team), "game_seconds_remaining": max(gsr, 1.0),
                             "half_seconds_remaining": 1000.0, "score_differential": 0.0, "wp": rng.uniform(0.1, 0.9),
                             "xpass": rng.uniform(0.3, 0.8), "pass_oe": 0.0, "no_huddle": float(rng.random() < 0.1),
                             "fixed_drive": float(i // 6), "drive_time_of_possession": f"{rng.integers(1, 5)}:{rng.integers(10, 59)}",
                             "drive_play_count": 6.0, "fixed_drive_result": "Punt"})
    return pd.DataFrame(rows)


def _features(games, pbp, hl=C.HALF_LIFE_PRIMARY):
    gt = aggregate_style_to_game_team(pbp, games)
    return add_phase3a_features(normalize_schedule_teams(games), gt, half_life=hl).set_index("game_id")


@pytest.fixture(scope="module")
def base():
    g = fake_games(); return g, fake_style_pbp(g)


@pytest.mark.parametrize("hl", [C.HALF_LIFE_PRIMARY, C.HALF_LIFE_SENSITIVITY])
def test_future_corruption_does_not_change_past_features(base, hl):
    games, pbp = base
    f0 = _features(games, pbp, hl)
    order = games.sort_values(["season", "week", "gameday", "game_id"]).game_id.tolist()
    cut = order[len(order) // 2]
    future = set(order[order.index(cut) + 1:])
    g2 = games.copy(); g2.loc[g2.game_id.isin(future), ["home_score", "away_score"]] = 999
    p2 = pbp.copy(); fm = p2.game_id.isin(future)
    p2.loc[fm, ["epa", "yards_gained", "success", "xpass", "no_huddle"]] = [50.0, 99.0, 1.0, 0.0, 1.0]
    p2.loc[fm, "drive_time_of_possession"] = "9:59"
    p2.loc[fm, "game_seconds_remaining"] = p2.loc[fm, "game_seconds_remaining"] * 0.5
    f1 = _features(g2, p2, hl)
    past = [x for x in order if x not in future]
    cols = EW_COLS + P3_COLS
    pd.testing.assert_frame_equal(f0.loc[past, cols], f1.loc[past, cols], check_exact=True)
    # sanity: the corruption does reach later games, so the test is not vacuous
    assert not np.allclose(f0.loc[list(future)[:20], "form_pts_home"], f1.loc[list(future)[:20], "form_pts_home"])


def test_own_game_data_never_used_for_own_features(base):
    games, pbp = base
    order = games.sort_values(["season", "week", "gameday", "game_id"]).game_id.tolist()
    target = order[len(order) // 3]
    f0 = _features(games, pbp)
    g2 = games.copy(); g2.loc[g2.game_id == target, ["home_score", "away_score"]] = 777
    p2 = pbp.copy(); m = p2.game_id == target
    p2.loc[m, ["epa", "yards_gained"]] = [40.0, 80.0]; p2.loc[m, "drive_time_of_possession"] = "9:00"
    f1 = _features(g2, p2)
    upto = order[: order.index(target) + 1]
    pd.testing.assert_frame_equal(f0.loc[upto, EW_COLS + P3_COLS], f1.loc[upto, EW_COLS + P3_COLS], check_exact=True)


def test_season_prior_uses_only_previous_season(base):
    games, pbp = base
    gt = aggregate_style_to_game_team(pbp, games)
    last = int(gt.season.max())
    gt2 = gt.copy(); gt2.loc[gt2.season == last, TEAM_METRICS] = 1e6
    p0, p1 = season_priors(gt), season_priors(gt2)
    for s in range(int(gt.season.min()), last + 1):
        assert p0[s] == p1[s], f"prior for season {s} moved when season {last} data changed"
    assert p0[last + 1] != p1[last + 1]


def test_reliability_windows():
    g = fake_games(n_seasons=4, start=2004); p = fake_style_pbp(g)
    gt = aggregate_style_to_game_team(p, g)
    for m, first in C.RELIABLE_FROM.items():
        assert gt.loc[gt.season < first, m].isna().all(), m
        assert gt.loc[gt.season >= first, m].notna().any(), m


def test_franchise_codes_join_pbp_to_schedule():
    g = fake_games(n_seasons=2, start=2010)
    g.loc[g.home_team == "T1", "home_team"] = "OAK"; g.loc[g.away_team == "T1", "away_team"] = "OAK"
    p = fake_style_pbp(g)
    p.loc[p.posteam == "OAK", "posteam"] = "LV"; p.loc[p.defteam == "OAK", "defteam"] = "LV"
    gt = aggregate_style_to_game_team(p, g)
    lv = gt[gt.team == "LV"]
    assert len(lv) > 0 and lv.off_epa.notna().all(), "pbp rows labelled LV must attach to schedule rows labelled OAK"
    assert "OAK" not in set(gt.team)


def test_generic_walk_forward_reproduces_ridge8_v1():
    g = fake_elo_games(n_seasons=8, games_per_season=80, seed=5)
    g["total_line"] = 44.0
    a = walk_forward_total(g, min_train_games=100, window=16)
    df = build_total_features(g, window=16)
    b = walk_forward_generic(df, FEATURE_COLS, standardize=False, min_train_games=100)
    a = a.set_index("game_id").sort_index(); b = b.set_index("game_id").sort_index()
    np.testing.assert_allclose(a.total_mean_pred.values, b.total_mean_pred.values, atol=1e-12, equal_nan=True)
    np.testing.assert_allclose(a.total_sigma_pred.values, b.total_sigma_pred.values, atol=1e-12, equal_nan=True)


def _write_cache(d, games, pbp_epa, style):
    d.mkdir(parents=True, exist_ok=True)
    games.to_parquet(d / "schedules.parquet", index=False)
    pbp_epa.to_parquet(d / "pbp_epa.parquet", index=False)
    style.to_parquet(d / "pbp_style.parquet", index=False)


def test_candidate_harness_ignores_holdout_and_future(tmp_path):
    from src.nfl.backtest.phase3a_harness import CANDIDATES, run_candidates

    def make(n):
        g = fake_games(n_seasons=n, per_season=80, start=2012, seed=3); g["roof"] = "outdoors"
        return g, fake_pbp_for_games(g, seed=3), fake_style_pbp(g, seed=3, plays_per_team=14)
    ga, ea, sa = make(9)                                  # 2012-2020
    gb, eb, sb = make(15)                                 # 2012-2026: includes holdout + future
    fut = gb.season >= 2021
    gb.loc[fut, ["home_score", "away_score"]] = 999
    sb.loc[sb.season >= 2021, "epa"] = 50.0
    _write_cache(tmp_path / "a", ga, ea, sa); _write_cache(tmp_path / "b", gb, eb, sb)
    cands = {k: CANDIDATES[k] for k in ("p3a_c0b_ridge8_std_teamfix", "p3a_c5_all")}
    run_candidates(tmp_path / "oa", raw_dir=tmp_path / "a", holdout_start=2021, dev=(2015, 2020), candidates=cands)
    run_candidates(tmp_path / "ob", raw_dir=tmp_path / "b", holdout_start=2021, dev=(2015, 2020), candidates=cands)
    for cid in cands:
        pa = pd.read_csv(tmp_path / "oa" / f"{cid}_predictions_dev_2015_2020.csv").set_index("game_id").sort_index()
        pb = pd.read_csv(tmp_path / "ob" / f"{cid}_predictions_dev_2015_2020.csv").set_index("game_id").sort_index()
        assert len(pa) > 0 and list(pa.index) == list(pb.index)
        np.testing.assert_allclose(pa.total_mean_pred.values, pb.total_mean_pred.values, atol=1e-9)
        assert pb.season.max() <= 2020
