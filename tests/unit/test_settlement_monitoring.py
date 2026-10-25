"""Safeguard 1: settlement monitoring checks every expected key (snapshot file, game, variant) in its own ledger.

Fixtures only. Historical records are never modified: the monitor and the verifier are read-only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.nfl.ops import monitor as M
from src.nfl.reporting import ledger as L
from tests.unit._phase4_fixtures import SCHED
from tests.unit.test_ledger_legacy import LEGACY_SNAP, SNAP_FILE as LEGACY_FILE, write_legacy_raw

UTC = timezone.utc
KICK = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
LATE = KICK + timedelta(days=3, hours=1)   # Wed Oct 21 2 PM ET: past Tue 10:30 settlement slot + 26 h

P4_GAME = {"game_id": "2026_07_SEA_DEN", "season": 2026, "week": 7, "away_team": "SEA", "home_team": "DEN",
           "market_total": 44.0, "market_source": "odds_snapshot:20261016T131000Z_thu_scheduled",
           "projected_total": 41.0, "edge": -3.0, "lean": "UNDER", "recommended_units": 0.0}
P4_SNAP_A = "totals_2026_wk07_20261016T132000Z.json"
P4_SNAP_B = "totals_2026_wk07_20261018T131500Z.json"


def _env(tmp_path, snaps: dict):
    odds, pred = tmp_path / "odds", tmp_path / "pred"; pred.mkdir()
    write_legacy_raw(odds)
    for name, snap in snaps.items():
        (pred / name).write_text(json.dumps(snap))
    return odds, pred, pred / "totals_ledger_v2.csv", pred / "totals_ledger_legacy_pre_spec.csv"


def _p4(game=P4_GAME, generated="2026-10-16T13:20:00+00:00"):
    return {"generated_at": generated, "model_version": "ridge8_v1", "games": [dict(game)]}


def _final(gid="2026_07_SEA_DEN", h=20, a=17, sched=SCHED):
    s = sched.copy()
    s.loc[s.game_id == gid, ["home_score", "away_score"]] = [h, a]
    return s


def _write_ledger(path, keys):
    pd.DataFrame(keys, columns=["snapshot_file", "game_id", "variant"]).to_csv(path, index=False)


def _alerts(env, sched, now=LATE):
    _, pred, v2, leg = env
    return M.unsettled(now, sched, pred, [v2, leg])


def test_legacy_row_present_but_phase4_rows_missing_is_an_alert(tmp_path):
    env = _env(tmp_path, {LEGACY_FILE: LEGACY_SNAP, P4_SNAP_A: _p4()})
    _write_ledger(env[3], [(LEGACY_FILE, "2026_07_SEA_DEN", "legacy_pre_spec")])
    out = _alerts(env, _final())
    assert len(out) == 1
    assert f"{P4_SNAP_A} / 2026_07_SEA_DEN / consensus_all" in out[0]["body"]
    assert f"{P4_SNAP_A} / 2026_07_SEA_DEN / best_available_ny" in out[0]["body"]
    assert LEGACY_FILE not in out[0]["body"]                                   # the legacy key is satisfied


def test_two_snapshots_for_one_game_one_missing(tmp_path):
    env = _env(tmp_path, {P4_SNAP_A: _p4(), P4_SNAP_B: _p4(generated="2026-10-18T13:15:00+00:00")})
    _write_ledger(env[2], [(P4_SNAP_A, "2026_07_SEA_DEN", "consensus_all"), (P4_SNAP_A, "2026_07_SEA_DEN", "best_available_ny")])
    gaps = L.settlement_gaps(_final(), env[1], env[2], env[3], LATE, 36)
    assert {(g["snapshot_file"], g["variant"]) for g in gaps} == {(P4_SNAP_B, "consensus_all"), (P4_SNAP_B, "best_available_ny")}


def test_one_missing_phase4_variant(tmp_path):
    env = _env(tmp_path, {P4_SNAP_A: _p4()})
    _write_ledger(env[2], [(P4_SNAP_A, "2026_07_SEA_DEN", "consensus_all")])
    gaps = L.settlement_gaps(_final(), env[1], env[2], env[3], LATE, 36)
    assert [(g["variant"], g["ledger"]) for g in gaps] == [("best_available_ny", "v2")]


def test_a_key_in_the_wrong_ledger_does_not_count(tmp_path):
    env = _env(tmp_path, {P4_SNAP_A: _p4()})
    _write_ledger(env[3], [(P4_SNAP_A, "2026_07_SEA_DEN", "consensus_all"), (P4_SNAP_A, "2026_07_SEA_DEN", "best_available_ny")])
    assert len(L.settlement_gaps(_final(), env[1], env[2], env[3], LATE, 36)) == 2


def test_unsettled_because_no_final_score_is_not_an_alert(tmp_path):
    env = _env(tmp_path, {P4_SNAP_A: _p4(), LEGACY_FILE: LEGACY_SNAP})
    assert _alerts(env, SCHED) == []                                             # nflverse has no score yet
    half = SCHED.copy()
    half.loc[half.game_id == "2026_07_SEA_DEN", ["home_score"]] = [20]          # partial score
    assert _alerts(env, half) == []
    assert _alerts(env, _final(), now=KICK + timedelta(hours=30)) == []          # final, but inside 36 h


def test_alerts_are_idempotent_and_clear_once_settled(tmp_path):
    env = _env(tmp_path, {P4_SNAP_A: _p4(), LEGACY_FILE: LEGACY_SNAP})
    a1, a2 = _alerts(env, _final()), _alerts(env, _final(), now=LATE + timedelta(hours=12))
    assert [f["title"] for f in a1] == [f["title"] for f in a2] == ["[collection-alert] Settlement missing for week 2026_07"]
    odds, pred, v2, leg = env
    L.record_results(_final(), pred_dir=pred, ledger=v2, odds_dir=odds, legacy_ledger=leg, now=LATE)
    assert _alerts(env, _final()) == []


@pytest.mark.parametrize("game", [
    P4_GAME,                                                                                  # odds lean: 2 variants
    {**P4_GAME, "lean": "PASS"},                                                              # PASS: none
    {**P4_GAME, "market_source": None},                                                       # no market: none
    {**P4_GAME, "market_source": "nflverse", "over_price": -110, "under_price": -110}])       # nflverse_line
def test_expected_keys_match_what_settlement_actually_writes(tmp_path, game):
    env = _env(tmp_path, {P4_SNAP_A: _p4(game), LEGACY_FILE: LEGACY_SNAP})
    odds, pred, v2, leg = env
    assert L.settlement_gaps(_final(), pred, v2, leg, LATE) != []
    L.record_results(_final(), pred_dir=pred, ledger=v2, odds_dir=odds, legacy_ledger=leg, now=LATE)
    assert L.settlement_gaps(_final(), pred, v2, leg, LATE) == []
    written = set(zip(*[pd.read_csv(f, dtype=str)[c] for f in (v2,) for c in ("snapshot_file", "game_id", "variant")]))
    which, variants = L.expected_settlement(_p4(game), game)
    assert which == "v2" and written == {(P4_SNAP_A, game["game_id"], v) for v in variants}


def test_monitor_and_verifier_never_modify_records(tmp_path):
    env = _env(tmp_path, {P4_SNAP_A: _p4(), LEGACY_FILE: LEGACY_SNAP})
    _write_ledger(env[2], [(P4_SNAP_A, "2026_07_SEA_DEN", "consensus_all")])
    before = {p.name: p.read_bytes() for p in env[1].iterdir()}
    _alerts(env, _final())
    L.settlement_gaps(_final(), env[1], env[2], env[3], LATE)
    assert {p.name: p.read_bytes() for p in env[1].iterdir()} == before


def test_settle_verify_command_passes_only_when_every_key_is_on_the_ledgers(tmp_path, monkeypatch):
    """The settlement heartbeat's read-back check (run.py settle-verify)."""
    import argparse
    import run as runner
    env = _env(tmp_path, {P4_SNAP_A: _p4(), LEGACY_FILE: LEGACY_SNAP})
    odds, pred, v2, leg = env
    monkeypatch.setattr("src.nfl.data.ingest.load_cached_schedules", lambda *a, **k: _final())
    args = argparse.Namespace(pred_dir=str(pred), as_of=(KICK + timedelta(hours=6)).isoformat())
    with pytest.raises(SystemExit) as e:
        runner.cmd_settle_verify(args)                       # nothing settled yet: missing keys
    assert e.value.code == 1
    L.record_results(_final(), pred_dir=pred, ledger=v2, odds_dir=odds, legacy_ledger=leg, now=KICK + timedelta(hours=6))
    runner.cmd_settle_verify(args)                           # every key present: returns normally
    # before the 5 h verified-final point nothing is expected yet, so an empty ledger also verifies
    runner.cmd_settle_verify(argparse.Namespace(pred_dir=str(tmp_path / "pred"), as_of=(KICK + timedelta(hours=4)).isoformat()))
