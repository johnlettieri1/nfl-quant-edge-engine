"""Phase 4: paired per-book quotes, timezone-safe matching, raw envelope, budget and cost guards."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.nfl.data import odds_snapshot as O
from tests.unit._phase4_fixtures import PAYLOAD, SCHED, book, event

SNAP_DT = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _parse(payload=PAYLOAD, dt=SNAP_DT, kind="thu_scheduled"):
    return O.parse_quotes(payload, "sid", dt, kind, SCHED, "raw.json", "abc")


def test_line_and_price_stay_paired_to_the_same_book():
    rows = _parse()
    assert [r["book"] for r in rows] == ["draftkings", "fanduel", "betmgm", "espnbet", "pinnacle", "bovada"]
    src = {b["key"]: b["markets"][0]["outcomes"] for b in PAYLOAD[0]["bookmakers"] if b["markets"][0]["key"] == "totals"}
    for r in rows:
        over, under = src[r["book"]]
        assert r["total_line"] == over["point"] == under["point"]
        assert (r["over_price"], r["under_price"]) == (over["price"], under["price"])


def test_matching_uses_eastern_calendar_date_not_fixed_offset():
    # 8:15 PM ET Thursday kickoff is 00:15 UTC Friday; the ET date is Thursday.
    sched = pd.DataFrame([{"game_id": "2026_06_SEA_DEN", "home_team": "DEN", "away_team": "SEA", "gameday": "2026-10-15"}])
    assert O.match_game_id("DEN", "SEA", "2026-10-16T00:15:00Z", sched) == ("2026_06_SEA_DEN", "team_et_date")
    # same wall-clock time after DST ends (EST): 8:15 PM ET = 01:15 UTC next day
    sched2 = pd.DataFrame([{"game_id": "2026_10_SEA_DEN", "home_team": "DEN", "away_team": "SEA", "gameday": "2026-11-12"}])
    assert O.match_game_id("DEN", "SEA", "2026-11-13T01:15:00Z", sched2) == ("2026_10_SEA_DEN", "team_et_date")
    assert O.match_game_id("DEN", "SEA", "2026-12-01T17:00:00Z", SCHED) == (None, "unmatched")
    assert O.match_game_id("DEN", "SEA", "2026-10-18T17:00:00Z", None) == (None, "no_schedule")


def test_unmapped_team_is_skipped_not_guessed():
    assert _parse([{**PAYLOAD[0], "home_team": "Denver Donkeys"}]) == []


def test_in_play_rows_are_flagged_not_pregame():
    after = datetime(2026, 10, 18, 17, 30, tzinfo=timezone.utc)
    rows = _parse(dt=after)
    assert rows and all(r["is_pregame"] is False and r["minutes_to_kickoff"] < 0 for r in rows)
    at_kick = datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc)
    assert all(r["is_pregame"] is False for r in _parse(dt=at_kick))


def test_moved_kickoff_uses_the_earlier_time():
    # Odds API still says 17:00 UTC, but nflverse says 1:00 PM ET -> 17:00; flex earlier to 16:00 UTC
    payload = [event(PAYLOAD[0]["bookmakers"][:2], commence="2026-10-18T16:00:00Z")]
    rows = _parse(payload, dt=datetime(2026, 10, 18, 16, 30, tzinfo=timezone.utc))
    assert all(r["is_pregame"] is False for r in rows)


def test_atomic_append_refuses_header_mismatch_and_torn_tail(tmp_path):
    p = tmp_path / "q.csv"
    O.append_rows_atomic(p, [{"a": 1, "b": 2}], ["a", "b"])
    O.append_rows_atomic(p, [{"a": 3, "b": 4}], ["a", "b"])
    assert p.read_text() == "a,b\n1,2\n3,4\n"
    with pytest.raises(RuntimeError, match="header"):
        O.append_rows_atomic(p, [{"a": 1, "c": 2}], ["a", "c"])
    p.write_text(p.read_text() + "5,")  # torn final row
    with pytest.raises(RuntimeError, match="torn"):
        O.append_rows_atomic(p, [{"a": 6, "b": 7}], ["a", "b"])


def test_raw_writes_are_exclusive_never_overwrite(tmp_path):
    p = tmp_path / "raw" / "totals_x.json"
    a = O.write_exclusive(p, b"first")
    b = O.write_exclusive(p, b"second")
    assert a.read_bytes() == b"first" and b.name == "totals_x-dup1.json" and b.read_bytes() == b"second"


class FakeAPI:
    def __init__(self, remaining=400, last="1", status=200, body=None):
        self.remaining, self.last, self.status, self.calls = remaining, last, status, 0
        self.body = body if body is not None else json.dumps(PAYLOAD).encode()

    def balance(self):
        return {"remaining": self.remaining, "used": 100}

    def totals(self, books):
        self.calls += 1
        assert len(books) <= 10
        return {"endpoint": "/v4/sports/americanfootball_nfl/odds",
                "request_params": {"markets": "totals", "bookmakers": ",".join(books)},
                "http_status": self.status, "retrieval_started_at": "2026-10-16T11:59:59+00:00",
                "fetched_at": "2026-10-16T12:00:00+00:00", "body": self.body,
                "x_requests_remaining": str(self.remaining - 1), "x_requests_used": "101", "x_requests_last": self.last}


@pytest.fixture
def api(monkeypatch):
    f = FakeAPI()
    monkeypatch.setattr(O, "fetch_balance", f.balance)
    monkeypatch.setattr(O, "fetch_totals_raw", f.totals)
    monkeypatch.setenv("ODDS_API_KEY", "SECRET-KEY-123")
    return f


def test_snapshot_writes_v2_envelope_with_verifiable_hash_and_no_key(tmp_path, api):
    s = O.take_snapshot("thu_scheduled", "schedule", SCHED, odds_dir=tmp_path, now=SNAP_DT)
    raw = tmp_path / "raw" / "totals_20261016T120000Z_thu_scheduled.json"
    env = json.loads(raw.read_text())
    assert env["schema_version"] == "odds_raw_v2" and env["snapshot_kind"] == "thu_scheduled"
    assert hashlib.sha256(env["response_body"].encode()).hexdigest() == env["response_sha256"]
    assert "SECRET-KEY-123" not in raw.read_text() and "apiKey" not in raw.read_text()
    q = pd.read_csv(tmp_path / "quotes" / "quotes_20261016T120000Z_thu_scheduled.csv")
    assert list(q.columns) == O.QUOTE_FIELDS and len(q) == 6 and s["quote_rows"] == 6
    assert len(O.load_quotes(tmp_path)) == 6
    assert set(q.snapshot_id) == {"20261016T120000Z_thu_scheduled"}
    assert s["games_for_weather"] == [("2026_07_SEA_DEN", "2026-10-18T17:00:00+00:00")]


def test_budget_guard_refuses_below_reserve_without_spending(tmp_path, api):
    api.remaining = 59
    with pytest.raises(O.BudgetError, match="reserve"):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP_DT)
    assert api.calls == 0 and not (tmp_path / "raw").exists()


def test_weekly_cap_refuses_without_spending(tmp_path, api):
    raw = tmp_path / "raw"; raw.mkdir()
    for i in range(30):  # 30 pulls since Tuesday Oct 13 00:00 ET
        (raw / f"totals_202610{14 + i // 24:02d}T{i % 24:02d}0000Z_pregame_window.json").write_text("{}")
    with pytest.raises(O.BudgetError, match="weekly cap"):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP_DT)
    assert api.calls == 0


def test_cost_anomaly_keeps_data_and_stops(tmp_path, api):
    api.last = "3"                                                  # expected 2 (totals + spreads)
    with pytest.raises(O.CostAnomalyError):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP_DT)
    assert len(list((tmp_path / "raw").glob("totals_*.json"))) == 1 and len(O.load_quotes(tmp_path)) == 6
    assert O.halt_files(tmp_path)                                   # HALT marker written
    api.last = "2"
    with pytest.raises(O.BudgetError, match="halted"):             # every later pull refused, nothing spent
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP_DT)
    assert api.calls == 1


def test_invalid_kinds_rejected(tmp_path, api):
    for k in ("opener", "current", "closing", "legacy_current"):
        with pytest.raises(ValueError):
            O.take_snapshot(k, schedules=SCHED, odds_dir=tmp_path, now=SNAP_DT)


def test_historical_raw_snapshots_are_never_rewritten(tmp_path, api):
    O.take_snapshot("thu_scheduled", schedules=SCHED, odds_dir=tmp_path, now=SNAP_DT)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    api.body = json.dumps([event([book("draftkings", 44.5, -110, 44.5, -110)])]).encode()
    O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP_DT)
    for rel, data in before.items():
        assert (tmp_path / rel).read_bytes() == data                # every earlier file byte-identical
    assert len(list((tmp_path / "quotes").glob("*.csv"))) == 2


def test_legacy_v1_raw_is_reparsed_as_legacy_current(tmp_path):
    raw = tmp_path / "raw"; raw.mkdir()
    (raw / "totals_20261015T180351Z_current.json").write_text(json.dumps(
        {"fetched_at": "2026-10-15T18:03:51+00:00", "tag": "current", "requests_remaining": "499", "games": PAYLOAD}))
    q = O.quotes_from_raw(tmp_path, SCHED)
    assert set(q.snapshot_kind) == {"legacy_current"} and len(q) == 6
    assert set(q.snapshot_id) == {"20261015T180351Z_legacy_current"}


def test_missing_balance_header_fails_closed(tmp_path, api, monkeypatch):
    monkeypatch.setattr(O, "fetch_balance", lambda: {"remaining": None, "used": None})
    with pytest.raises(O.BudgetError, match="fail closed"):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP_DT)
    assert api.calls == 0


KICK = "2026-10-18T17:00:00+00:00"
TARGET = [{"event_id": "evt1", "kickoff": KICK}]


def _quotes_file(odds_dir, ts, rows):
    q = odds_dir / "quotes"; q.mkdir(parents=True, exist_ok=True)
    body = "snapshot_ts_utc,event_id,book,total_line,over_price,under_price\n" + "".join(
        f"{ts},{eid},{book},42.5,-110,-110\n" for eid, book in rows)
    (q / f"quotes_{ts.replace(':', '').replace('-', '')[:15]}Z_pregame_window.csv").write_text(body)


def test_delayed_pregame_job_never_pays_after_kickoff(tmp_path, api):
    for late in (datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc),       # exactly at kickoff
                 datetime(2026, 10, 18, 17, 1, tzinfo=timezone.utc),       # after kickoff
                 datetime(2026, 10, 18, 16, 57, tzinfo=timezone.utc)):     # inside T-5
        with pytest.raises(O.PregameNotNeeded):
            O.take_snapshot("pregame_window", schedules=SCHED, odds_dir=tmp_path, targets=TARGET, now=late)
    assert api.calls == 0 and not (tmp_path / "raw").exists()


def test_recheck_skips_when_the_game_was_already_quoted_in_window(tmp_path, api):
    raw = tmp_path / "raw"; raw.mkdir()
    (raw / "totals_20261018T164000Z_pregame_window.json").write_text("{}")
    _quotes_file(tmp_path, "2026-10-18T16:40:00+00:00", [("evt1", "draftkings"), ("evt1", "fanduel")])
    with pytest.raises(O.PregameNotNeeded):
        O.take_snapshot("pregame_window", schedules=SCHED, odds_dir=tmp_path, targets=TARGET,
                        now=datetime(2026, 10, 18, 16, 45, tzinfo=timezone.utc))
    assert api.calls == 0


def test_recheck_pays_when_the_snapshot_did_not_quote_this_game(tmp_path, api):
    raw = tmp_path / "raw"; raw.mkdir()
    (raw / "totals_20261018T164000Z_pregame_window.json").write_text("{}")
    _quotes_file(tmp_path, "2026-10-18T16:40:00+00:00", [("evtOTHER", "draftkings")])
    s = O.take_snapshot("pregame_window", schedules=SCHED, odds_dir=tmp_path, targets=TARGET,
                        now=datetime(2026, 10, 18, 16, 45, tzinfo=timezone.utc))
    assert api.calls == 1 and s["quote_rows"] == 6


def test_pregame_without_targets_never_pays(tmp_path, api):
    with pytest.raises(O.PregameNotNeeded):
        O.take_snapshot("pregame_window", schedules=SCHED, odds_dir=tmp_path, targets=None, now=SNAP_DT)
    assert api.calls == 0


# ---- crash-safe immutable writes ----------------------------------------------------------------

def test_existing_records_cannot_be_overwritten(tmp_path):
    p = tmp_path / "raw" / "totals_x.json"
    O.write_exclusive(p, b"original")
    for i in range(3):
        O.write_exclusive(p, f"other{i}".encode())
    assert p.read_bytes() == b"original"
    assert sorted(x.name for x in p.parent.iterdir()) == ["totals_x-dup1.json", "totals_x-dup2.json", "totals_x-dup3.json", "totals_x.json"]


def test_interrupted_write_never_exposes_a_partial_file(tmp_path, monkeypatch):
    p = tmp_path / "raw" / "totals_y.json"
    real_fsync = os.fsync
    def boom(fd):
        raise OSError("simulated power loss during fsync")
    monkeypatch.setattr(O.os, "fsync", boom)
    with pytest.raises(OSError):
        O.write_exclusive(p, b"x" * 100_000)
    monkeypatch.setattr(O.os, "fsync", real_fsync)
    assert not p.exists()                                       # no final file at all
    assert list(p.parent.iterdir()) == []                       # and no partial temp file either


def test_crash_before_publish_leaves_no_final_file(tmp_path):
    """A real process that dies (os._exit) after writing the temp file but before publishing."""
    import subprocess, sys
    code = (f"import os, sys; sys.path.insert(0, {str(pathlib.Path.cwd())!r}); from src.nfl.data import odds_snapshot as O; "
            "O.os.link = lambda a, b: os._exit(9); "
            f"O.write_exclusive(__import__('pathlib').Path({str(tmp_path / 'raw' / 'totals_z.json')!r}), b'payload' * 1000)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert r.returncode == 9
    files = sorted(x.name for x in (tmp_path / "raw").iterdir())
    assert "totals_z.json" not in files and len(files) == 1 and files[0].startswith(".tmp-")
    assert O.raw_names(tmp_path) == []                          # readers never see the leftover temp file
    from src.nfl.ops.records_guard import evaluate
    leftover = f"data/odds/raw/{files[0]}"
    assert evaluate([("A", leftover)], lambda _: b"", lambda _: b"x")   # guard rejects it


def test_append_rows_atomic_is_crash_safe(tmp_path, monkeypatch):
    p = tmp_path / "ledger.csv"
    O.append_rows_atomic(p, [{"a": 1, "b": 2}], ["a", "b"])
    before = p.read_bytes()
    monkeypatch.setattr(O.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        O.append_rows_atomic(p, [{"a": 3, "b": 4}], ["a", "b"])
    assert p.read_bytes() == before and [x.name for x in tmp_path.iterdir()] == ["ledger.csv"]


def test_response_hash_still_verifies_after_atomic_publish(tmp_path, api):
    O.take_snapshot("thu_scheduled", "schedule", SCHED, odds_dir=tmp_path, now=SNAP_DT)
    env = json.loads((tmp_path / "raw" / "totals_20261016T120000Z_thu_scheduled.json").read_text())
    assert hashlib.sha256(env["response_body"].encode()).hexdigest() == env["response_sha256"]
    snap = next(O.iter_raw_snapshots(tmp_path))
    assert snap["hash_ok"] is True
    assert not [x for x in tmp_path.rglob(".tmp-*")]            # no temp files left after success


def test_legacy_file_is_not_counted_as_a_phase4_pull():
    from src.nfl.market.timing import pulls_this_week
    assert pulls_this_week(SNAP_DT, ["totals_20261015T180351Z_current.json", "totals_20261015T225438Z_manual.json"]) == 1
