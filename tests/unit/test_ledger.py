"""Phase 4: ledger v2 settlement, line/price-correct CLV, 0 units, append-only and idempotent."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from src.nfl.data import odds_snapshot as O
from src.nfl.reporting import ledger
from tests.unit._phase4_fixtures import SCHED, book, event

UTC = timezone.utc


def test_settle():
    assert ledger.settle("OVER", 44.5, 45) == "win" and ledger.settle("OVER", 44.5, 44) == "loss"
    assert ledger.settle("UNDER", 44.0, 44) == "push" and ledger.settle("PASS", 44.0, 50) == "none"
    assert ledger.settle("OVER", None, 50) == "none"


def _write_raw(odds_dir, dt, kind, books):
    raw = odds_dir / "raw"; raw.mkdir(parents=True, exist_ok=True)
    body = json.dumps([event(books)])
    (raw / f"totals_{dt:%Y%m%dT%H%M%SZ}_{kind}.json").write_text(json.dumps({
        "schema_version": "odds_raw_v2", "snapshot_kind": kind, "fetched_at": dt.isoformat(), "http_status": 200,
        "response_sha256": __import__("hashlib").sha256(body.encode()).hexdigest(), "response_body": body}))


def test_record_results_v2(tmp_path):
    odds, pred = tmp_path / "odds", tmp_path / "pred"; pred.mkdir()
    s = datetime(2026, 10, 16, 13, 10, tzinfo=UTC)
    _write_raw(odds, s, "thu_scheduled", [book("draftkings", 44.0, -110, 44.0, -110), book("fanduel", 44.5, -105, 44.5, -115),
                                          book("betmgm", 44.0, -112, 44.0, -108)])
    _write_raw(odds, datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window",
               [book("draftkings", 42.5, -110, 42.5, -110), book("fanduel", 42.5, -110, 42.5, -110), book("betmgm", 43.0, -110, 43.0, -110),
                book("espnbet", 42.5, -110, 42.5, -110), book("pinnacle", 42.5, -105, 42.5, -105)])
    snap = {"generated_at": "2026-10-16T13:20:00+00:00", "model_version": "ridge8_v1", "games": [
        {"game_id": "2026_07_SEA_DEN", "season": 2026, "week": 7, "away_team": "SEA", "home_team": "DEN",
         "market_total": 44.0, "market_source": "odds_snapshot:20261016T131000Z_thu_scheduled",
         "projected_total": 41.0, "edge": -3.0, "lean": "UNDER", "raw_model_probability": 0.3,
         "calibrated_probability": 0.44, "recommended_units": 0.0},
        {"game_id": "2026_07_DET_MIN", "season": 2026, "week": 7, "away_team": "DET", "home_team": "MIN",
         "market_total": 48.0, "over_price": -110, "under_price": -110, "market_source": "nflverse",
         "projected_total": 49.0, "edge": 1.0, "lean": "OVER", "recommended_units": 0.0}]}
    (pred / "totals_2026_wk07_20261016T132000Z.json").write_text(json.dumps(snap))
    sched = SCHED.copy()
    sched.loc[sched.game_id == "2026_07_SEA_DEN", ["home_score", "away_score"]] = [20, 17]
    sched.loc[sched.game_id == "2026_07_DET_MIN", ["home_score", "away_score"]] = [30, 24]
    led = tmp_path / "ledger.csv"
    assert ledger.record_results(sched, pred_dir=pred, ledger=led, odds_dir=odds) == 3
    assert ledger.record_results(sched, pred_dir=pred, ledger=led, odds_dir=odds) == 0     # idempotent
    df = pd.read_csv(led)
    assert list(df.columns) == ledger.FIELDS and (df.units_risked == 0).all()
    assert not any(c.startswith(("closing", "opening")) for c in df.columns)
    ny = df[(df.game_id == "2026_07_SEA_DEN") & (df.variant == "best_available_ny")].iloc[0]
    assert (ny.line_taken, ny.book_taken, ny.ref_canonical_line) == (44.5, "fanduel", 42.5)
    assert ny.line_clv == 2.0 and ny.result == "win" and bool(ny.ref_close_quality) is True
    assert pd.isna(ny.price_clv_same_book)                      # 44.5 not posted at R: blank, not manufactured
    cons = df[(df.game_id == "2026_07_SEA_DEN") & (df.variant == "consensus_all")].iloc[0]
    assert cons.line_taken == 44.0 and cons.line_clv == 1.5 and ny.first_observed_line == 44.0
    nfl = df[df.game_id == "2026_07_DET_MIN"].iloc[0]
    assert nfl.variant == "nflverse_line" and pd.isna(nfl.line_clv) and nfl.result == "win"
    assert json.loads((pred / "totals_2026_wk07_20261016T132000Z.json").read_text()) == snap    # snapshot untouched
