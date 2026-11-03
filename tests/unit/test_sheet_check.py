"""Phase 5C pick-sheet support: sign conventions, edge and key-number flags, confidence rule, team aliases,
card validation, and append-only idempotent settlement (incl. pushes)."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from src.nfl.market import sheet_check as S


def test_sheet_home_spread_convention():
    assert S.sheet_home_spread("DET", "DET", 3.5) == -3.5      # home favored -> negative home number
    assert S.sheet_home_spread("MIA", "NE", 7.5) == 7.5        # away favored -> positive home number
    with pytest.raises(ValueError):
        S.sheet_home_spread("DET", "DET", -3.5)


def test_cover_and_push():
    assert S.cover(-3, 3.5, "home") == "W"        # home +3.5 loses by 3: covers
    assert S.cover(-3, 3.0, "home") == "P"
    assert S.cover(-3, 3.5, "away") == "L"
    assert S.cover(7, -7.0, "home") == "P" and S.cover(7, -6.5, "home") == "W"


def test_edge_and_key_numbers_match_week8_card():
    # MIN @ DET: sheet DET -3.5, DK DET -3 -> +0.5 for the Vikings (away), crosses 3
    side, pts = S.edge(-3.5, -3.0)
    assert (side, pts) == ("away", 0.5) and S.key_crossed(-3.5, -3.0) == [3]
    # ARI @ DAL: sheet DAL -6.5, DK -7 -> +0.5 for the Cowboys (home), crosses 7
    assert S.edge(-6.5, -7.0) == ("home", 0.5) and S.key_crossed(-6.5, -7.0) == [7]
    # IND @ JAX: sheet -7.5 vs -10: 2.5 for the Jaguars, no 3 or 7 crossed, 10 is minor
    assert S.edge(-7.5, -10.0) == ("home", 2.5) and S.key_crossed(-7.5, -10.0) == []
    assert S.key_crossed(-7.5, -10.0, S.MINOR_KEYS) == [10]
    assert S.edge(-2.5, -2.5) == (None, 0.0) and S.key_crossed(-2.5, -2.5) == []


def test_rule_confidence():
    assert S.rule_confidence(2.5, False) == "High"
    assert S.rule_confidence(0.5, True) == "Medium"
    assert S.rule_confidence(1.0, False) == "Medium"
    assert S.rule_confidence(0.5, False) == "Low"
    assert S.rule_confidence(0.0, True) == "Low" and S.rule_confidence(None, False) == "Low"
    assert S.rule_confidence(2.5, False, qb_question=True) == "Low"     # QB question caps at Low


def test_aliases_and_bad_favorite():
    sh = pd.DataFrame([{"sheet_order": 1, "away_team": "lac", "home_team": "LAR", "fav_team": "LAR", "line": "6.5"}])
    out = S.normalize_sheet(sh)
    assert out.iloc[0].home_team == "LA" and out.iloc[0].fav_team == "LA" and out.iloc[0].away_team == "LAC"
    sh.loc[0, "fav_team"] = "DAL"
    with pytest.raises(ValueError):
        S.normalize_sheet(sh)


def _quotes():
    rows = []
    for ts, hs in (("2026-11-05T13:00:00+00:00", -3.0), ("2026-11-07T13:00:00+00:00", -2.5)):
        for book, adj in (("draftkings", 0.0), ("fanduel", 0.5), ("pinnacle", 0.0), ("bovada", 1.0)):
            rows.append({"snapshot_id": ts[:13], "snapshot_ts_utc": ts, "game_id": "2026_10_MIN_DET",
                         "home_team": "DET", "away_team": "MIN", "is_pregame": True, "minutes_to_kickoff": 600,
                         "book": book, "home_spread": hs + adj, "away_spread": -(hs + adj)})
    return pd.DataFrame(rows)


def _sheet(**extra):
    base = {"sheet_order": 1, "away_team": "MIN", "home_team": "DET", "fav_team": "DET", "line": "3.5"}
    return pd.DataFrame([{**base, **extra}])


def test_market_view_respects_as_of_and_scopes():
    chk = S.check_sheet(_sheet(), _quotes(), datetime(2026, 11, 6, tzinfo=timezone.utc))
    r = chk.iloc[0]
    assert r.dk_home_spread_at_check == -3.0 and r.first_dk_home_spread == -3.0     # later snapshot ignored
    assert r.best_ny_home_spread == -2.5                                             # fanduel; bovada is offshore
    assert r.pinnacle_home_spread == -3.0
    assert (r.edge_side, r.edge_pts, r.key_flag) == ("away", 0.5, "3")
    chk2 = S.check_sheet(_sheet(), _quotes(), datetime(2026, 11, 8, tzinfo=timezone.utc))
    assert chk2.iloc[0].dk_home_spread_at_check == -2.5 and chk2.iloc[0].first_dk_home_spread == -3.0


def test_card_validation():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    sh = _sheet(pick_team="MIN", confidence="rule", favorite_pick="yes")
    card = S.build_card(sh, S.check_sheet(sh, _quotes(), now), 2026, 10, now)
    r = card.iloc[0]
    assert r.pick_side == "away" and r.pick_line_display == "+3.5" and r.pick_edge_pts == 0.5
    assert r.rule_confidence == "Medium" and r.confidence == "Medium" and r.dk_source == "snapshot"
    with pytest.raises(ValueError):                       # pick must be one of the two teams
        bad = _sheet(pick_team="GB", confidence="Low", favorite_pick="yes")
        S.build_card(bad, S.check_sheet(bad, _quotes(), now), 2026, 10, now)
    with pytest.raises(ValueError):                       # exactly one favorite pick
        nofav = _sheet(pick_team="MIN", confidence="Low", favorite_pick="")
        S.build_card(nofav, S.check_sheet(nofav, _quotes(), now), 2026, 10, now)


def test_settlement_append_only_idempotent(tmp_path):
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    odds = tmp_path / "odds" / "spread_quotes"; odds.mkdir(parents=True)
    q = _quotes()
    for c in ("snapshot_kind", "event_id"):
        q[c] = ""
    q.to_csv(odds / "spread_quotes_x.csv", index=False)
    sh = _sheet(pick_team="MIN", confidence="rule", favorite_pick="yes")
    card = S.build_card(sh, S.check_sheet(sh, q, now), 2026, 10, now)
    pred = tmp_path / "pred"
    path = S.write_card(card, pred)
    with pytest.raises(FileExistsError):
        S.write_card(card, pred)                         # cards are immutable
    sched = pd.DataFrame([{"game_id": "2026_10_MIN_DET", "home_score": 24, "away_score": 21,
                           "gameday": "2026-11-08", "gametime": "13:00"}])
    late = datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert S.settle_sheets(sched, datetime(2026, 11, 8, 18, tzinfo=timezone.utc), pred, tmp_path / "odds") == 0
    assert S.settle_sheets(sched, late, pred, tmp_path / "odds") == 1
    assert S.settle_sheets(sched, late, pred, tmp_path / "odds") == 0
    led = pd.read_csv(pred / "sheet_ledger.csv")
    r = led.iloc[0]
    assert r.card_file == path.name and r.margin == 3
    assert r.result_sheet == "W" and r.result_dk_check == "P" and r.result_dk_close == "L"   # +3.5 / +3 / +2.5
    assert S.summarize_sheet(pred / "sheet_ledger.csv")["sheet_edge_changed_result"] == 1
