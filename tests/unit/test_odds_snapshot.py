"""Phase 2B: per-book totals parsing, best-number convention, schedule matching."""
from __future__ import annotations

import pandas as pd

from src.nfl.data.odds_snapshot import match_game_id, parse_totals_payload

PAYLOAD = [{
    "id": "evt1", "commence_time": "2026-10-18T17:00:00Z",
    "home_team": "Denver Broncos", "away_team": "Seattle Seahawks",
    "bookmakers": [
        {"key": "draftkings", "markets": [{"key": "totals", "last_update": "t", "outcomes": [
            {"name": "Over", "price": -110, "point": 42.5}, {"name": "Under", "price": -110, "point": 42.5}]}]},
        {"key": "fanduel", "markets": [{"key": "totals", "last_update": "t", "outcomes": [
            {"name": "Over", "price": -105, "point": 42.0}, {"name": "Under", "price": -115, "point": 42.0}]}]},
        {"key": "betmgm", "markets": [{"key": "totals", "last_update": "t", "outcomes": [
            {"name": "Over", "price": -112, "point": 43.0}, {"name": "Under", "price": -108, "point": 43.0}]}]},
        {"key": "caesars", "markets": [{"key": "totals", "last_update": "t", "outcomes": [
            {"name": "Over", "price": -115, "point": 43.0}, {"name": "Under", "price": -105, "point": 43.0}]}]},
        {"key": "broken", "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "price": -110, "point": 44.0}, {"name": "Under", "price": -110, "point": 43.5}]}]},
        {"key": "spreads_only", "markets": [{"key": "spreads", "outcomes": [
            {"name": "Denver Broncos", "price": -110, "point": -3.0}, {"name": "Seattle Seahawks", "price": -110, "point": 3.0}]}]},
    ],
}]
SCHED = pd.DataFrame([{"game_id": "2026_07_SEA_DEN", "home_team": "DEN", "away_team": "SEA", "gameday": "2026-10-18"},
                      {"game_id": "2026_01_SEA_DEN", "home_team": "DEN", "away_team": "SEA", "gameday": "2026-09-10"}])


def test_parse_per_book_rows_and_summary():
    books, summary = parse_totals_payload(PAYLOAD, "20261016T120000Z", "opener", SCHED)
    assert [b["book"] for b in books] == ["draftkings", "fanduel", "betmgm", "caesars"]  # malformed + non-totals skipped
    assert all(b["game_id"] == "2026_07_SEA_DEN" for b in books)
    s = summary[0]
    assert s["n_books"] == 4 and s["lowest_total"] == 42.0 and s["highest_total"] == 43.0
    assert s["market_median"] == 42.75
    # Over bettor wants the lowest number: fanduel 42.0 at -105
    assert (s["best_over_line"], s["best_over_price"], s["best_over_book"]) == (42.0, -105, "fanduel")
    # Under bettor wants the highest number; tie at 43.0 broken by price: caesars -105 beats betmgm -108
    assert (s["best_under_line"], s["best_under_price"], s["best_under_book"]) == (43.0, -105, "caesars")
    assert abs(s["consensus_over_prob"] + s["consensus_under_prob"] - 1.0) < 1e-9
    assert s["tag"] == "opener" and s["matched_schedule"] is True


def test_match_uses_date_window_not_just_teams():
    assert match_game_id("DEN", "SEA", "2026-10-18T17:00:00Z", SCHED) == "2026_07_SEA_DEN"
    assert match_game_id("DEN", "SEA", "2026-12-01T17:00:00Z", SCHED) is None
    assert match_game_id("DEN", "SEA", "2026-10-18T17:00:00Z", None) is None


def test_unmapped_team_is_skipped_not_guessed():
    bad = [{**PAYLOAD[0], "home_team": "Denver Donkeys"}]
    books, summary = parse_totals_payload(bad, "ts", "current", SCHED)
    assert books == [] and summary == []
