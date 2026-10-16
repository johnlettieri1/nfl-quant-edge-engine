"""Week 6 ledger compatibility hotfix: legacy pre-spec predictions (fixtures only, no API calls)."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.nfl.ops.records_guard import main as guard_main
from src.nfl.reporting import ledger
from src.nfl.reporting import legacy_ledger as LL
from tests.unit._phase4_fixtures import SCHED, book, event

UTC = timezone.utc
LEGACY_TS = "20261015T180351Z"
LEGACY_BOOKS = [book("draftkings", 43.0, -110, 43.0, -110), book("fanduel", 43.0, -105, 43.0, -115),
                book("betmgm", 43.5, -110, 43.5, -110), book("lowvig", 42.5, -104, 42.5, -106),
                book("bovada", 43.0, -110, 43.0, -110)]                     # median 43.0
LATER_BOOKS = [book(b, 41.5, -110, 41.5, -110) for b in ("draftkings", "fanduel", "betmgm", "espnbet", "ballybet")]


def write_legacy_raw(odds):
    raw = odds / "raw"; raw.mkdir(parents=True, exist_ok=True)
    (raw / f"totals_{LEGACY_TS}_current.json").write_text(json.dumps(
        {"fetched_at": "2026-10-15T18:03:51.146583+00:00", "tag": "current", "requests_remaining": "499",
         "markets": "totals", "regions": "us", "games": [event(LEGACY_BOOKS)]}))


def write_v2_raw(odds, dt, kind, books):
    raw = odds / "raw"; raw.mkdir(parents=True, exist_ok=True)
    body = json.dumps([event(books)])
    (raw / f"totals_{dt:%Y%m%dT%H%M%SZ}_{kind}.json").write_text(json.dumps({
        "schema_version": "odds_raw_v2", "snapshot_kind": kind, "fetched_at": dt.isoformat(), "http_status": 200,
        "response_sha256": hashlib.sha256(body.encode()).hexdigest(), "response_body": body}))


LEGACY_GAME = {"game_id": "2026_07_SEA_DEN", "season": 2026, "week": 7, "away_team": "SEA", "home_team": "DEN",
               "market_total": 43.0, "market_source": f"odds_snapshot:{LEGACY_TS}", "n_books": 5,
               "over_price": -104, "under_price": -105, "best_over_line": 42.5, "best_over_price": -104,
               "best_over_book": "lowvig", "best_under_line": 43.5, "best_under_price": -110, "best_under_book": "betmgm",
               "projected_total": 40.9, "sigma": 13.1, "model_over_prob": 0.4369, "edge": -2.1, "lean": "UNDER",
               "recommended_units": 0.0}     # pre-spec: no raw_model_probability / calibrated_probability fields
LEGACY_SNAP = {"generated_at": "2026-10-15T18:04:06.742470+00:00", "season": 2026, "week": 7,
               "model_version": "ridge8_v1", "games": [LEGACY_GAME]}
SNAP_FILE = "totals_2026_wk07_20261015T180406Z.json"


@pytest.fixture
def env(tmp_path):
    odds, pred = tmp_path / "odds", tmp_path / "pred"; pred.mkdir()
    write_legacy_raw(odds)
    (pred / SNAP_FILE).write_text(json.dumps(LEGACY_SNAP))
    return tmp_path, odds, pred


def final(total_home, total_away):
    s = SCHED.copy()
    s.loc[s.game_id == "2026_07_SEA_DEN", ["home_score", "away_score"]] = [total_home, total_away]
    return s


def run(env, sched):
    tmp, odds, pred = env
    n = ledger.record_results(sched, pred_dir=pred, ledger=tmp / "v2.csv", odds_dir=odds,
                              legacy_ledger=tmp / "legacy.csv")
    return n, (pd.read_csv(tmp / "legacy.csv") if (tmp / "legacy.csv").exists() else pd.DataFrame())


# 1. legacy source identification -----------------------------------------------------------------
def test_legacy_source_resolves_to_exactly_one_original_snapshot(env):
    tmp, odds, _ = env
    r = LL.resolve_legacy_source(f"odds_snapshot:{LEGACY_TS}", odds)
    assert r["raw_file"] == f"totals_{LEGACY_TS}_current.json" and r["snapshot_id"] == f"{LEGACY_TS}_legacy_current"
    assert LL.resolve_legacy_source("odds_snapshot:20261015T999999Z", odds) is None          # no such file
    assert LL.resolve_legacy_source(f"odds_snapshot:{LEGACY_TS}_manual", odds) is None       # Phase 4 id, not legacy
    assert LL.resolve_legacy_source("nflverse", odds) is None
    (odds / "raw" / f"totals_{LEGACY_TS}_manual.json").write_text("{}")                     # ambiguous -> refuse
    assert LL.resolve_legacy_source(f"odds_snapshot:{LEGACY_TS}", odds) is None
    assert LL.is_legacy_snapshot(LEGACY_SNAP)
    assert not LL.is_legacy_snapshot({"games": [{"market_source": "odds_snapshot:20261016T131000Z_thu_scheduled"}]})


# 2 + 8. originals untouched, nothing rewritten -----------------------------------------------------
def test_original_prediction_and_all_records_unchanged(env):
    tmp, odds, pred = env
    write_v2_raw(odds, datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window", LATER_BOOKS)
    before = {p: p.read_bytes() for p in list(odds.rglob("*")) + list(pred.rglob("*")) if p.is_file()}
    run(env, final(20, 17))
    for p, b in before.items():
        assert p.read_bytes() == b
    assert json.loads((pred / SNAP_FILE).read_text()) == LEGACY_SNAP


# 3. research results --------------------------------------------------------------------------------
@pytest.mark.parametrize("home,away,expected", [(20, 17, "win"), (24, 23, "loss"), (23, 20, "push")])
def test_research_result_against_original_total(env, home, away, expected):
    _, df = run(env, final(home, away))
    r = df.iloc[0]
    assert (r.side, r.line_taken, r.result) == ("UNDER", 43.0, expected)
    assert r.result_type == "hypothetical_research_not_a_wager" and r.variant == "legacy_pre_spec"
    assert r.units_risked == 0


# 4 + 6. no synthetic quote; missing stays missing ----------------------------------------------------
def test_no_synthetic_sportsbook_quote_and_missing_fields_stay_blank(env):
    tmp, odds, _ = env
    write_v2_raw(odds, datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window", LATER_BOOKS)
    n, df = run(env, final(20, 17))
    r = df.iloc[0]
    assert pd.isna(r.price_taken) and pd.isna(r.book_taken) and pd.isna(r.price_clv)
    assert r.line_taken == LEGACY_GAME["market_total"]                     # the original number, nothing better
    assert r.line_basis == "legacy_median_all_observed_books_not_executable"
    assert bool(r.line_verified_against_raw) is True                      # recorded median matches the raw books
    assert pd.isna(r.calibrated_probability) and r.raw_model_probability == LEGACY_GAME["model_over_prob"]
    assert not (tmp / "v2.csv").exists()                                  # no consensus_all / best_available_ny rows
    assert n == 1


# 5. CLV only with a valid later pregame reference ------------------------------------------------------
def test_consensus_move_blank_without_later_reference(env):
    _, df = run(env, final(20, 17))
    r = df.iloc[0]
    assert pd.isna(r.consensus_line_move) and pd.isna(r.ref_snapshot_id) and pd.isna(r.ref_close_quality)


def test_consensus_move_with_valid_later_reference(env):
    tmp, odds, _ = env
    write_v2_raw(odds, datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window", LATER_BOOKS)
    write_v2_raw(odds, datetime(2026, 10, 18, 17, 30, tzinfo=UTC), "manual", [book("draftkings", 30.5, -110, 30.5, -110)])
    _, df = run(env, final(20, 17))
    r = df.iloc[0]
    assert r.ref_snapshot_id == "20261018T164000Z_pregame_window"           # in-play snapshot never used
    assert r.ref_median_line == 41.5 and r.consensus_line_move == 1.5      # UNDER at 43.0, market median fell to 41.5
    assert bool(r.ref_close_quality) is True and "espnbet" in r.ref_book_set
    assert pd.isna(r.price_clv)


def test_unresolved_legacy_id_keeps_result_but_no_market_comparison(env):
    tmp, odds, _ = env
    (odds / "raw" / f"totals_{LEGACY_TS}_current.json").unlink()
    write_v2_raw(odds, datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window", LATER_BOOKS)
    _, df = run(env, final(20, 17))
    r = df.iloc[0]
    assert r.source_resolution == "unresolved" and r.result == "win" and pd.isna(r.consensus_line_move)


# 7. idempotent ----------------------------------------------------------------------------------------
def test_settlement_is_idempotent(env):
    tmp, _, _ = env
    n1, _ = run(env, final(20, 17))
    b = (tmp / "legacy.csv").read_bytes()
    n2, _ = run(env, final(20, 17))
    assert (n1, n2) == (1, 0) and (tmp / "legacy.csv").read_bytes() == b


# 9. normal Phase 4 settlement unchanged alongside legacy -------------------------------------------------
def test_phase4_snapshots_still_use_v2_and_legacy_stays_separate(env):
    tmp, odds, pred = env
    s = datetime(2026, 10, 16, 13, 10, tzinfo=UTC)
    write_v2_raw(odds, s, "thu_scheduled", [book("draftkings", 44.0, -110, 44.0, -110), book("fanduel", 44.5, -105, 44.5, -115)])
    write_v2_raw(odds, datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window", LATER_BOOKS)
    p4 = {"generated_at": "2026-10-16T13:20:00+00:00", "model_version": "ridge8_v1", "games": [
        {**{k: v for k, v in LEGACY_GAME.items() if k not in ("over_price", "under_price")},
         "market_source": "odds_snapshot:20261016T131000Z_thu_scheduled", "market_total": 44.25,
         "market_total_basis": "median_all_observed_books", "raw_model_probability": 0.4, "calibrated_probability": 0.47}]}
    (pred / "totals_2026_wk07_20261016T132000Z.json").write_text(json.dumps(p4))
    n, df = run(env, final(20, 17))
    v2 = pd.read_csv(tmp / "v2.csv")
    assert list(v2.columns) == ledger.FIELDS                                    # v2 schema unchanged
    assert set(v2.snapshot_file) == {"totals_2026_wk07_20261016T132000Z.json"}
    assert set(v2.variant) == {"consensus_all", "best_available_ny"}
    assert list(df.columns) == LL.FIELDS and set(df.snapshot_file) == {SNAP_FILE} and n == 3


# 10. records guard ------------------------------------------------------------------------------------
def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def test_records_guard_accepts_new_legacy_ledger_and_protects_it(tmp_path):
    r = tmp_path / "records"; (r / "data/predictions").mkdir(parents=True)
    git(tmp_path, "init", "-q", str(r))
    (r / "data/predictions/.gitkeep").write_text("")
    git(r, "add", "-A"); git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    led = r / "data/predictions/totals_ledger_legacy_pre_spec.csv"
    led.write_text("h\n1\n"); git(r, "add", "-A")
    assert guard_main(["--repo", str(r)]) == 0                                  # first write: added
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "legacy")
    led.write_text("h\n1\n2\n"); git(r, "add", "-A")
    assert guard_main(["--repo", str(r)]) == 0                                  # append-only growth
    led.write_text("h\n9\n2\n"); git(r, "add", "-A")
    assert guard_main(["--repo", str(r)]) == 1                                  # rewrite refused


def test_no_negative_zero_in_consensus_move(env):
    tmp, odds, _ = env
    write_v2_raw(odds, datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window",
                 [book(b, 43.0, -110, 43.0, -110) for b in ("draftkings", "fanduel", "betmgm", "espnbet", "ballybet")])
    _, df = run(env, final(20, 17))
    assert str(df.iloc[0].consensus_line_move) == "0.0"
