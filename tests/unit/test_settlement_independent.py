"""Independent settlement (scheduling recovery): verified finals only, late scores, repeat runs, no Odds key."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.nfl.reporting import ledger
from tests.unit._phase4_fixtures import SCHED
from tests.unit.test_ledger_legacy import LEGACY_SNAP, SNAP_FILE, write_legacy_raw

UTC = timezone.utc
KICK = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)     # 1:00 PM EDT, both fixture games


def _env(tmp_path):
    odds, pred = tmp_path / "odds", tmp_path / "pred"; pred.mkdir()
    write_legacy_raw(odds)
    (pred / SNAP_FILE).write_text(json.dumps(LEGACY_SNAP))
    return odds, pred, tmp_path / "v2.csv", tmp_path / "legacy.csv"


def _sched(scores: dict):
    s = SCHED.copy()
    for gid, (h, a) in scores.items():
        s.loc[s.game_id == gid, ["home_score", "away_score"]] = [h, a]
    return s


def _run(paths, sched, now):
    odds, pred, v2, leg = paths
    return ledger.record_results(sched, pred_dir=pred, ledger=v2, odds_dir=odds, legacy_ledger=leg, now=now)


def test_missing_final_score_is_not_settled_and_later_run_picks_it_up(tmp_path):
    paths = _env(tmp_path)
    assert _run(paths, SCHED, KICK + timedelta(days=2)) == 0                 # Tuesday: nflverse has no score yet
    assert not paths[3].exists()
    late = _sched({"2026_07_SEA_DEN": (20, 17)})
    assert _run(paths, late, KICK + timedelta(days=3)) == 1                  # Wednesday retry: late final arrives
    assert _run(paths, late, KICK + timedelta(days=4)) == 0                  # Thursday retry: nothing new


def test_score_reported_too_soon_after_kickoff_is_not_trusted(tmp_path):
    paths = _env(tmp_path)
    s = _sched({"2026_07_SEA_DEN": (14, 10)})                                # in-progress-looking score
    assert _run(paths, s, KICK + timedelta(hours=2)) == 0
    assert _run(paths, s, KICK + timedelta(hours=4, minutes=59)) == 0
    assert _run(paths, s, KICK + timedelta(hours=5)) == 1


def test_partial_score_is_not_a_final():
    row = pd.Series({"home_score": 21, "away_score": None, "gameday": "2026-10-18", "gametime": "13:00"})
    assert not ledger.is_verified_final(row, KICK + timedelta(days=3))
    row = pd.Series({"home_score": 21, "away_score": 17, "gameday": "2026-10-18", "gametime": None})
    assert ledger.is_verified_final(row, KICK + timedelta(days=3))           # date-only fallback still waits 5 h


def test_repeated_and_concurrent_style_runs_never_duplicate_or_rewrite(tmp_path):
    paths = _env(tmp_path)
    s = _sched({"2026_07_SEA_DEN": (20, 17)})
    now = KICK + timedelta(days=2)
    assert _run(paths, s, now) == 1
    before = paths[3].read_bytes()
    for _ in range(4):                       # external + cron twins + manual
        assert _run(paths, s, now) == 0
    assert paths[3].read_bytes() == before
    df = pd.read_csv(paths[3])
    assert len(df) == len(df.drop_duplicates(["snapshot_file", "game_id", "variant"]))


def test_already_recorded_rows_are_preserved_byte_for_byte(tmp_path):
    """Week 6 style: an existing ledger with rows already recorded is only ever appended to."""
    paths = _env(tmp_path)
    s = _sched({"2026_07_SEA_DEN": (20, 17)})
    _run(paths, s, KICK + timedelta(days=2))
    recorded = paths[3].read_bytes()
    # a second prediction snapshot later in the week for another game settles into the same file
    snap2 = json.loads(json.dumps(LEGACY_SNAP))
    snap2["games"][0]["game_id"] = "2026_07_DET_MIN"
    snap2["games"][0]["home_team"], snap2["games"][0]["away_team"] = "MIN", "DET"
    (paths[1] / "totals_2026_wk07_20261015T190000Z.json").write_text(json.dumps(snap2))
    s2 = _sched({"2026_07_SEA_DEN": (99, 99), "2026_07_DET_MIN": (30, 24)})  # a changed score cannot rewrite history
    assert _run(paths, s2, KICK + timedelta(days=3)) == 1
    new = paths[3].read_bytes()
    assert new.startswith(recorded)
    df = pd.read_csv(paths[3])
    assert df[df.game_id == "2026_07_SEA_DEN"].actual_total.tolist() == [37.0]


def test_settle_command_never_needs_the_odds_key(tmp_path, monkeypatch):
    import run as runner
    called = {}
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setattr("src.nfl.data.ingest.fetch_schedules", lambda *a, **k: SCHED)
    monkeypatch.setattr("src.nfl.reporting.ledger.record_results", lambda s: called.setdefault("n", 0))
    monkeypatch.setattr("src.nfl.reporting.ledger.summarize", lambda: {"rows": 0})

    def boom(*a, **k):
        raise AssertionError("settlement must not touch the Odds API")
    monkeypatch.setattr("src.nfl.data.odds_api.fetch_totals_raw", boom)
    monkeypatch.setattr("src.nfl.data.odds_api.fetch_balance", boom)
    runner.cmd_record_results(None)
    assert called == {"n": 0}
