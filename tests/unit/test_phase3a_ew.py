"""Hand-computed checks of the Phase 3A EW estimator and possession formula."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.nfl.features import phase3a_config as C
from src.nfl.features.phase3a_features import TEAM_METRICS, add_phase3a_features


def _games(scores, seasons):
    rows = []
    for i, ((hs, as_), s) in enumerate(zip(scores, seasons)):
        rows.append({"game_id": f"{s}_{i:02d}", "season": s, "week": i + 1, "gameday": pd.Timestamp(f"{s}-09-01") + pd.Timedelta(days=7 * i),
                     "home_team": "AAA", "away_team": "BBB", "home_score": hs, "away_score": as_})
    return pd.DataFrame(rows)


def _game_team(games):
    rows = []
    for g in games.itertuples(index=False):
        for team, pf, pa in (("AAA", g.home_score, g.away_score), ("BBB", g.away_score, g.home_score)):
            r = {m: np.nan for m in TEAM_METRICS}; r.update({"game_id": g.game_id, "season": g.season, "team": team,
                                                              "off_pts": pf, "def_pts": pa, "drive_secs": 150.0, "drive_secs_faced": 150.0})
            rows.append(r)
    return pd.DataFrame(rows)


def test_ew_matches_formula_within_season():
    g = _games([(30, 10), (20, 10), (0, 0)], [2010, 2010, 2010])
    f = add_phase3a_features(g, _game_team(g), half_life=6.0, carryover=0.5, k=4.0).set_index("game_id")
    d = 0.5 ** (1 / 6)
    prior = C.DEFAULTS["off_pts"]  # no 2009 data -> fixed default
    assert f.loc["2010_00", "home_off_pts_ew"] == prior
    assert np.isclose(f.loc["2010_01", "home_off_pts_ew"], (4 * prior + 30) / (4 + 1))
    assert np.isclose(f.loc["2010_02", "home_off_pts_ew"], (4 * prior + 20 + d * 30) / (4 + 1 + d))


def test_offseason_carryover_and_prior_season_mean():
    g = _games([(30, 10), (20, 10)], [2010, 2011])
    f = add_phase3a_features(g, _game_team(g), half_life=6.0, carryover=0.5, k=4.0).set_index("game_id")
    prior_2011 = (30 + 10) / 2  # league mean of off_pts over 2010 team-games
    assert np.isclose(f.loc["2011_01", "home_off_pts_ew"], (4 * prior_2011 + 0.5 * 30) / (4 + 0.5))


def test_possessions_formula():
    g = _games([(30, 10), (20, 10)], [2010, 2010])
    f = add_phase3a_features(g, _game_team(g)).set_index("game_id")
    # no 2009 data -> league prior is the 160 s default; each team's estimate after one 150 s game
    ew = (4 * 160.0 + 150.0) / 5
    t = ew * ew / 160.0
    assert np.isclose(f.loc["2010_01", "exp_poss_home"], 3600.0 / (2 * t))
    assert np.isclose(f.loc["2010_01", "exp_game_possessions"], 2 * 3600.0 / (2 * t))
    assert np.isclose(f.loc["2010_00", "exp_game_possessions"], 2 * 3600.0 / 320.0)
