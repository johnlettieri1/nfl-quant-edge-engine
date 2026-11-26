"""Phase 5D: body-clock definitions, the test's cover logic, holdout exclusion, and the sheet flag."""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from src.nfl.backtest import phase5d_west_coast as T
from src.nfl.market import body_clock as B
from src.nfl.market import sheet_check as S

NIGHT, FOUR, ONE = 20 * 60 + 20, 16 * 60 + 25, 13 * 60


def test_team_definitions():
    assert B.is_pacific("SEA", 2005) and B.is_pacific("SD", 2010) and B.is_pacific("OAK", 2015) and B.is_pacific("LV", 2022)
    assert B.is_pacific("LA", 2016) and not B.is_pacific("LA", 2015) and not B.is_pacific("STL", 2010)
    for mountain_or_central in ("DEN", "ARI", "KC", "DAL", "CHI"):
        assert not B.is_pacific(mountain_or_central, 2020) and mountain_or_central not in B.EASTERN
    assert {"IND", "DET", "NE", "MIA"} <= B.EASTERN


def test_window_edges():
    assert B.window_for(19 * 60) == "night" and B.window_for(22 * 60 + 59) == "night" and B.window_for(23 * 60) is None
    assert B.window_for(16 * 60) == "4pm" and B.window_for(18 * 60 + 59) == "4pm" and B.window_for(15 * 60 + 59) is None
    assert B.window_for(None) is None and B.window_for(float("nan")) is None and B.window_for(ONE) is None


def test_classify():
    assert B.classify("PIT", "SEA", 2021, NIGHT) == {"window": "night", "west_team": "SEA", "west_home": False}
    assert B.classify("LAC", "MIA", 2022, NIGHT) == {"window": "night", "west_team": "LAC", "west_home": True}
    assert B.classify("SF", "NYG", 2020, FOUR)["window"] == "4pm"
    assert B.classify("SEA", "NYG", 2020, ONE) is None                    # outside both windows
    assert B.classify("SEA", "DEN", 2020, NIGHT) is None                  # Mountain opponent
    assert B.classify("DEN", "NE", 2020, NIGHT) is None                   # Mountain is not West Coast
    assert B.classify("SEA", "SF", 2020, NIGHT) is None                   # West vs West
    assert B.classify("PIT", "SEA", 2021, NIGHT, neutral=True) is None    # neutral site
    assert B.classify("NE", "LA", 2015, NIGHT) is None                    # Rams were in St. Louis


def _sched():
    base = {"game_type": "REG", "location": "Home", "weekday": "Sunday", "gameday": "2020-10-01", "week": 5}
    rows = [
        # West home favored by 3 (spread_line +3), wins by 7 -> West covers
        {"game_id": "a", "season": 2020, "home_team": "SEA", "away_team": "NE", "gametime": "20:20", "home_score": 27, "away_score": 20, "spread_line": 3.0},
        # West away, home favored by 3, home wins by 1 -> West (+3) covers
        {"game_id": "b", "season": 2010, "home_team": "PIT", "away_team": "SD", "gametime": "20:30", "home_score": 21, "away_score": 20, "spread_line": 3.0},
        # West away, home favored by 3, home wins by 3 -> push
        {"game_id": "c", "season": 2012, "home_team": "NYG", "away_team": "SF", "gametime": "20:30", "home_score": 23, "away_score": 20, "spread_line": 3.0},
        # West home favored by 7, wins by 3 -> West fails to cover (but wins straight up)
        {"game_id": "d", "season": 2018, "home_team": "LA", "away_team": "PHI", "gametime": "16:25", "home_score": 23, "away_score": 20, "spread_line": 7.0},
        # holdout season, neutral site, Mountain team, playoff, 1 PM game: all excluded
        {"game_id": "e", "season": 2023, "home_team": "SEA", "away_team": "NE", "gametime": "20:20", "home_score": 30, "away_score": 0, "spread_line": 3.0},
        {"game_id": "f", "season": 2019, "home_team": "SEA", "away_team": "NE", "gametime": "20:20", "home_score": 30, "away_score": 0, "spread_line": 3.0, "location": "Neutral"},
        {"game_id": "g", "season": 2019, "home_team": "DEN", "away_team": "NE", "gametime": "20:20", "home_score": 30, "away_score": 0, "spread_line": 3.0},
        {"game_id": "h", "season": 2019, "home_team": "SEA", "away_team": "PHI", "gametime": "20:20", "home_score": 30, "away_score": 0, "spread_line": 3.0, "game_type": "WC"},
        {"game_id": "i", "season": 2019, "home_team": "NE", "away_team": "SEA", "gametime": "13:00", "home_score": 30, "away_score": 0, "spread_line": 3.0},
    ]
    return pd.DataFrame([{**base, **r} for r in rows])


def test_build_games_cover_logic_and_exclusions():
    g = T.build_games(_sched()).set_index("game_id")
    assert list(g.index) == ["a", "b", "c", "d"]
    assert g.loc["a", "west_cover"] == "W" and g.loc["a", "west_spread"] == 3.0
    assert g.loc["b", "west_cover"] == "W" and g.loc["b", "west_spread"] == -3.0 and g.loc["b", "west_su"] == "L"
    assert g.loc["c", "west_cover"] == "P"
    assert g.loc["d", "west_cover"] == "L" and g.loc["d", "west_su"] == "W" and g.loc["d", "window"] == "4pm"
    s = T.summarize(T.build_games(_sched()))
    assert s["night"]["all"]["covers"] == 2 and s["night"]["all"]["pushes"] == 1 and s["4pm"]["PASS"] is None


def test_results_file_has_no_holdout_and_matches_registration():
    p = Path("docs/phase5/results/phase5d_west_coast_games.csv")
    if not p.exists():
        pytest.skip("results not generated in this checkout")
    rows = list(csv.DictReader(p.open()))
    assert max(int(r["season"]) for r in rows) < T.HOLDOUT_START and min(int(r["season"]) for r in rows) >= 2000
    assert sum(r["window"] == "night" for r in rows) == 69 and sum(r["window"] == "4pm" for r in rows) == 297


def test_sheet_flag_is_informational_only():
    def quotes(kick):
        return pd.DataFrame([{"snapshot_id": "s", "snapshot_ts_utc": "2026-11-27T13:00:00+00:00", "game_id": "2026_12_LV_CLE",
                              "home_team": "CLE", "away_team": "LV", "is_pregame": True, "minutes_to_kickoff": 3000,
                              "book": "draftkings", "home_spread": -3.0, "away_spread": 3.0, "kickoff_utc_resolved": kick}])
    sheet = pd.DataFrame([{"sheet_order": 1, "away_team": "LV", "home_team": "CLE", "fav_team": "CLE", "line": "3.5"}])
    night = S.check_sheet(sheet, quotes("2026-11-30T01:20:00+00:00")).iloc[0]        # 8:20 PM ET
    assert night.body_clock_spot.startswith("West Coast body-clock spot (night): LV [tested, not supported")
    day = S.check_sheet(sheet, quotes("2026-11-29T18:00:00+00:00")).iloc[0]          # 1:00 PM ET
    assert day.body_clock_spot == ""
    # the flag changes nothing else on the row
    for c in ("edge_side", "edge_pts", "key_flag", "stale_flag", "dk_home_spread_at_check"):
        assert night[c] == day[c]
    assert S.check_sheet(sheet, quotes("2026-11-30T01:20:00+00:00"), neutral_ids=frozenset({"2026_12_LV_CLE"})).iloc[0].body_clock_spot == ""
    assert "body-clock" in S.format_table(S.check_sheet(sheet, quotes("2026-11-30T01:20:00+00:00")))
