"""Phase 2B: ledger settlement and CLV math."""
from __future__ import annotations

import json

import pandas as pd

from src.nfl.reporting import ledger


def test_settle_and_clv_points():
    assert ledger.settle("OVER", 44.5, 45) == "win"
    assert ledger.settle("OVER", 44.5, 44) == "loss"
    assert ledger.settle("UNDER", 44.0, 44) == "push"
    assert ledger.settle("PASS", 44.0, 50) == "none"
    # Over bettor took 44.5, market closed 46: beat the close by 1.5
    assert ledger.clv_points("OVER", 44.5, 46.0) == 1.5
    # Under bettor took 44.5, market closed 46: lost 1.5 to the close
    assert ledger.clv_points("UNDER", 44.5, 46.0) == -1.5
    assert ledger.clv_points("OVER", 44.5, None) is None


def test_clv_price_and_pnl():
    assert ledger.implied_prob(-110) - 0.5238 < 1e-3 and ledger.implied_prob(100) == 0.5
    # took -105, closed -115: closing implied prob is higher, so you got the better price (positive)
    assert ledger.clv_price(-105, -115) > 0
    assert ledger.pnl("win", -110, 1.0) == round(100 / 110, 4)
    assert ledger.pnl("loss", -110, 1.0) == -1.0
    assert ledger.pnl("push", -110, 1.0) == 0.0
    assert ledger.pnl("win", -110, 0.0) == 0.0


def test_record_results_is_append_only_and_idempotent(tmp_path, monkeypatch):
    pred = tmp_path / "pred"; pred.mkdir()
    snap = {"generated_at": "2026-10-16T12:00:00+00:00", "model_version": "ridge8_v1", "games": [
        {"game_id": "2026_07_SEA_DEN", "season": 2026, "week": 7, "away_team": "SEA", "home_team": "DEN",
         "market_total": 42.5, "over_price": -110, "under_price": -110, "market_source": "nflverse",
         "projected_total": 40.1, "lean": "UNDER", "recommended_units": 0.0},
        {"game_id": "2026_07_X_Y", "season": 2026, "week": 7, "away_team": "X", "home_team": "Y",
         "market_total": 50.0, "over_price": -110, "under_price": -110, "market_source": "nflverse",
         "projected_total": 52.0, "lean": "OVER", "recommended_units": 0.0}]}
    (pred / "totals_2026_wk07_20261016T120000Z.json").write_text(json.dumps(snap))
    sched = pd.DataFrame([{"game_id": "2026_07_SEA_DEN", "home_score": 20, "away_score": 17, "total_line": 43.5, "over_odds": -108, "under_odds": -112},
                          {"game_id": "2026_07_X_Y", "home_score": None, "away_score": None, "total_line": 50.0, "over_odds": -110, "under_odds": -110}])
    monkeypatch.setattr(ledger, "load_game_summary", lambda *a, **k: pd.DataFrame())
    led = tmp_path / "ledger.csv"
    assert ledger.record_results(sched, pred_dir=pred, ledger=led) == 1   # unplayed game not recorded
    assert ledger.record_results(sched, pred_dir=pred, ledger=led) == 0   # idempotent
    df = pd.read_csv(led)
    assert list(df.columns) == ledger.FIELDS
    r = df.iloc[0]
    assert r.result == "win" and r.actual_total == 37 and r.closing_line == 43.5 and r.closing_source == "nflverse_closing"
    assert r.clv_points == -1.0  # Under at 42.5, closed 43.5: the close moved against the Under
    assert r.units_risked == 0 and r.units_won_lost == 0
    # snapshot untouched
    assert json.loads((pred / "totals_2026_wk07_20261016T120000Z.json").read_text()) == snap
