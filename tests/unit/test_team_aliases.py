"""Regression tests for the relocated-team alias fix (src/nfl/data/team_aliases.py).

Proves: (1) the three historical aliases resolve to the current franchise codes and
nothing else changes; (2) the original defect is real (an OAK schedule row never
picks up EPA history from LV-labelled play-by-play) and the fix removes it; (3) on
the real 1999-2022 cache, every normalized schedule team code is present in that
game's play-by-play, while un-normalized STL/SD/OAK codes are not; (4) the frozen
ridge8_v1 feature path does not apply the fix (its behavior stays Phase 2A).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.nfl.data.team_aliases import RELOCATED_TEAM_ALIASES, normalize_schedule_team_codes, normalize_team_code
from src.nfl.data.team_mapping import ODDS_API_TEAM_TO_NFLVERSE
from src.nfl.features.epa_features import LEAGUE_AVG_EPA, add_trailing_epa_features
from tests.leakage._helpers import fake_elo_games, fake_pbp_for_games

RAW = Path("data/raw")


def test_historical_aliases_resolve_to_current_franchise():
    assert normalize_team_code("STL") == "LA"
    assert normalize_team_code("SD") == "LAC"
    assert normalize_team_code("OAK") == "LV"
    assert RELOCATED_TEAM_ALIASES == {"STL": "LA", "SD": "LAC", "OAK": "LV"}


def test_current_codes_unchanged_and_idempotent():
    current = set(ODDS_API_TEAM_TO_NFLVERSE.values())
    assert len(current) == 32
    for c in current:
        assert normalize_team_code(c) == c
    for a in RELOCATED_TEAM_ALIASES:
        assert normalize_team_code(normalize_team_code(a)) == normalize_team_code(a)
    assert pd.isna(normalize_team_code(np.nan))


def test_schedule_normalization_only_touches_team_columns():
    g = pd.DataFrame({"home_team": ["OAK", "KC", "SD"], "away_team": ["STL", "LV", "DEN"], "roof": ["OAK", "x", "y"]})
    n = normalize_schedule_team_codes(g)
    assert n.home_team.tolist() == ["LV", "KC", "LAC"]
    assert n.away_team.tolist() == ["LA", "LV", "DEN"]
    assert n.roof.tolist() == ["OAK", "x", "y"]          # other columns untouched
    assert g.home_team.tolist() == ["OAK", "KC", "SD"]   # input not mutated


def _relabelled_fixture():
    g = fake_elo_games(n_seasons=2, games_per_season=40, seed=11)
    g.loc[g.home_team == "T1", "home_team"] = "OAK"
    g.loc[g.away_team == "T1", "away_team"] = "OAK"
    pbp = fake_pbp_for_games(g, seed=11)
    pbp.loc[pbp.posteam == "OAK", "posteam"] = "LV"     # pbp uses the current code, as nflverse does
    pbp.loc[pbp.defteam == "OAK", "defteam"] = "LV"
    return g, pbp


def test_defect_reproduced_without_fix():
    g, pbp = _relabelled_fixture()
    f = add_trailing_epa_features(g, pbp, window=5)
    oak_home = f[f.home_team == "OAK"]
    assert len(oak_home) > 5
    # the bug: OAK never accumulates history, so its own trailing EPA stays at the default
    assert (oak_home.home_off_epa_trailing == LEAGUE_AVG_EPA).all()


def test_fix_restores_history():
    g, pbp = _relabelled_fixture()
    f = add_trailing_epa_features(normalize_schedule_team_codes(g), pbp, window=5)
    lv_home = f[f.home_team == "LV"].iloc[3:]          # after a few games of history
    assert len(lv_home) > 0
    assert (lv_home.home_off_epa_trailing != LEAGUE_AVG_EPA).all()


@pytest.mark.skipif(not ((RAW / "schedules.parquet").exists() and (RAW / "pbp_epa.parquet").exists()),
                    reason="needs the local 1999-2022 cache (python run.py data --last-season 2022)")
def test_real_cache_every_normalized_code_matches_pbp():
    sched = pd.read_parquet(RAW / "schedules.parquet", columns=["game_id", "season", "home_team", "away_team"])
    sched = sched[sched.season < 2023]                   # holdout never touched
    pbp = pd.read_parquet(RAW / "pbp_epa.parquet", columns=["game_id", "posteam"])
    pbp = pbp[pbp.game_id.str[:4].astype(int) < 2023]
    teams_in_game = pbp.dropna(subset=["posteam"]).groupby("game_id").posteam.agg(set)
    s = sched[sched.game_id.isin(teams_in_game.index)]
    n = normalize_schedule_team_codes(s)
    tig = teams_in_game.loc[n.game_id]
    ok = [h in t and a in t for h, a, t in zip(n.home_team, n.away_team, tig)]
    assert all(ok), f"{len(ok) - sum(ok)} games still unmatched after normalization"
    raw_ok = [h in t and a in t for h, a, t in zip(s.home_team, s.away_team, tig)]
    alias_games = s.home_team.isin(RELOCATED_TEAM_ALIASES) | s.away_team.isin(RELOCATED_TEAM_ALIASES)
    assert alias_games.sum() > 800             # 877 STL/SD/OAK games in 1999-2022
    assert not any(np.array(raw_ok)[alias_games.to_numpy()]), "un-normalized alias games should never match (the defect)"


def test_frozen_ridge8_v1_path_does_not_apply_fix():
    """ridge8_v1 must keep Phase 2A behavior: its feature builders keep schedule codes as given."""
    from src.nfl.features.qb_features import add_trailing_qb_features
    from src.nfl.models.total.total_model import build_total_features
    g, pbp = _relabelled_fixture()
    g["total_line"] = 44.0
    out = build_total_features(add_trailing_qb_features(add_trailing_epa_features(g, pbp, window=5), pbp, window=5), window=16)
    assert "OAK" in set(out.home_team) and "LV" not in set(out.home_team)
