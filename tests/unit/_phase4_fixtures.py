"""Shared synthetic fixtures for Phase 4 tests (no network, no real credits)."""
from __future__ import annotations

import pandas as pd

SCHED = pd.DataFrame([
    {"game_id": "2026_07_SEA_DEN", "season": 2026, "week": 7, "home_team": "DEN", "away_team": "SEA",
     "gameday": "2026-10-18", "gametime": "13:00", "home_score": None, "away_score": None,
     "stadium_id": "DEN00", "stadium": "Empower Field at Mile High", "total_line": 43.0, "over_odds": -110, "under_odds": -110},
    {"game_id": "2026_01_SEA_DEN", "season": 2026, "week": 1, "home_team": "DEN", "away_team": "SEA",
     "gameday": "2026-09-10", "gametime": "20:15", "home_score": 20, "away_score": 17,
     "stadium_id": "DEN00", "stadium": "Empower Field at Mile High", "total_line": 41.0, "over_odds": -110, "under_odds": -110},
    {"game_id": "2026_07_DET_MIN", "season": 2026, "week": 7, "home_team": "MIN", "away_team": "DET",
     "gameday": "2026-10-18", "gametime": "13:00", "home_score": None, "away_score": None,
     "stadium_id": "MIN01", "stadium": "U.S. Bank Stadium", "total_line": 48.0, "over_odds": -110, "under_odds": -110},
])


def book(key, over_pt, over_px, under_pt, under_px, lu="2026-10-16T11:58:00Z"):
    return {"key": key, "markets": [{"key": "totals", "last_update": lu, "outcomes": [
        {"name": "Over", "price": over_px, "point": over_pt}, {"name": "Under", "price": under_px, "point": under_pt}]}]}


def event(books, eid="evt1", commence="2026-10-18T17:00:00Z", home="Denver Broncos", away="Seattle Seahawks"):
    return {"id": eid, "commence_time": commence, "home_team": home, "away_team": away, "bookmakers": books}


PAYLOAD = [event([
    book("draftkings", 42.5, -110, 42.5, -110),
    book("fanduel", 42.0, -105, 42.0, -115),
    book("betmgm", 43.0, -112, 43.0, -108),
    book("espnbet", 43.0, -115, 43.0, -105),
    book("pinnacle", 42.5, -104, 42.5, -106),
    book("bovada", 41.5, -110, 41.5, -110),
    book("broken", 44.0, -110, 43.5, -110),                                   # mismatched points: skipped
    {"key": "spreads_only", "markets": [{"key": "spreads", "outcomes": [
        {"name": "Denver Broncos", "price": -110, "point": -3.0}, {"name": "Seattle Seahawks", "price": -110, "point": 3.0}]}]},
])]
