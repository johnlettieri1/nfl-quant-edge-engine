"""Collection monitor: each alert fires from the records themselves, stays silent when healthy, never duplicates."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.nfl.ops import monitor as M
from tests.unit._phase4_fixtures import SCHED

UTC = timezone.utc
START = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)


def u(*a):
    return datetime(*a, tzinfo=UTC)


def raw(dt, kind):
    return f"totals_{dt:%Y%m%dT%H%M%SZ}_{kind}.json"


# one full healthy week of scheduled captures (Thu Oct 22 .. Tue Oct 27), all on time
HEALTHY = [raw(u(2026, 10, 22, 13, 11), "thu_scheduled"), raw(u(2026, 10, 24, 13, 11), "sat_scheduled"),
           raw(u(2026, 10, 25, 13, 11), "sun_scheduled"), raw(u(2026, 10, 26, 3, 31), "sun_night_scheduled"),
           raw(u(2026, 10, 27, 14, 11), "tue_scheduled")]


def test_scheduled_captures_present_means_no_alert():
    assert M.missed_scheduled(u(2026, 10, 28, 12, 0), HEALTHY, START) == []


def test_missing_scheduled_snapshot_alerts_only_after_its_window_closes():
    have = [n for n in HEALTHY if "sat_scheduled" not in n]
    # Sat 11 PM ET: sat window (to Sun 3:10 AM ET) still open, a delayed run may still land -> silent
    assert M.missed_scheduled(u(2026, 10, 25, 3, 0), have, START) == []
    out = M.missed_scheduled(u(2026, 10, 28, 12, 0), have, START)
    assert [f["key"] for f in out] == ["missed-snapshot-sat_scheduled-20261024"]


def test_delayed_capture_inside_window_counts_as_served():
    have = [n for n in HEALTHY if "sat_scheduled" not in n] + [raw(u(2026, 10, 24, 20, 0), "sat_scheduled")]
    assert M.missed_scheduled(u(2026, 10, 28, 12, 0), have, START) == []


def test_capture_after_window_does_not_hide_a_miss():
    # a manual capture labeled sat_scheduled on Sunday afternoon is outside the sat window
    have = [n for n in HEALTHY if "sat_scheduled" not in n] + [raw(u(2026, 10, 25, 19, 0), "sat_scheduled")]
    assert len(M.missed_scheduled(u(2026, 10, 28, 12, 0), have, START)) == 1


def test_nothing_before_monitor_start_is_reported():
    assert M.missed_scheduled(u(2026, 10, 21, 12, 0), [], START) == []


def _quotes(rows):
    return pd.DataFrame(rows, columns=["game_id", "snapshot_ts_utc"])


def test_missed_pregame_by_game_and_slot():
    now = u(2026, 10, 21, 12, 0)
    start = u(2026, 10, 18, 0, 0)
    q = _quotes([("2026_07_SEA_DEN", "2026-10-18T16:40:00+00:00"),          # T-20: covered
                 ("2026_07_DET_MIN", "2026-10-18T15:00:00+00:00"),          # T-120: not a pregame close
                 ("2026_07_DET_MIN", "2026-10-18T17:05:00+00:00")])         # after kickoff: never counts
    out = M.missed_pregame(now, q, SCHED, start)
    assert len(out) == 1 and "2026_07_DET_MIN" in out[0]["body"] and "SEA_DEN" not in out[0]["body"]
    q2 = _quotes([("2026_07_SEA_DEN", "2026-10-18T16:40:00+00:00"), ("2026_07_DET_MIN", "2026-10-18T16:45:00+00:00")])
    assert M.missed_pregame(now, q2, SCHED, start) == []


def test_future_and_in_progress_games_are_not_missed_yet():
    q = _quotes([])
    assert M.missed_pregame(u(2026, 10, 18, 16, 59), q, SCHED, u(2026, 10, 18, 0, 0)) == []


def _pred(tmp_path):
    pred = tmp_path / "predictions"; pred.mkdir()
    (pred / "totals_2026_wk07_x.json").write_text(json.dumps({"games": [{"game_id": "2026_07_SEA_DEN"},
                                                                         {"game_id": "2026_07_DET_MIN"}]}))
    (pred / "totals_index.json").write_text("{}")
    return pred


def test_unsettled_final_games_alert_after_36h_and_clear_when_settled(tmp_path):
    pred = _pred(tmp_path)
    s = SCHED.copy()
    s.loc[s.game_id == "2026_07_SEA_DEN", ["home_score", "away_score"]] = [20, 17]
    led, leg = pred / "totals_ledger_v2.csv", pred / "totals_ledger_legacy_pre_spec.csv"
    assert M.unsettled(u(2026, 10, 19, 17, 0), s, pred, [led, leg]) == []           # only 24 h after kickoff
    out = M.unsettled(u(2026, 10, 20, 6, 0), s, pred, [led, leg])
    assert [f["key"] for f in out] == ["unsettled-2026_07"] and "DET_MIN" not in out[0]["body"]   # no score: not final
    led.write_text("snapshot_file,game_id,variant\ntotals_2026_wk07_x.json,2026_07_SEA_DEN,none\n")
    assert M.unsettled(u(2026, 10, 20, 6, 0), s, pred, [led, leg]) == []


def test_halt_alert():
    assert [f["key"] for f in M.halt(["HALT_cost_anomaly_20261025T131000Z.json", raw(u(2026, 10, 25, 13, 10), "x")])] == \
        ["halt-HALT_cost_anomaly_20261025T131000Z.json"]
    assert M.halt(HEALTHY) == []


def test_failed_workflows_alert_recent_failures_only():
    now = u(2026, 10, 25, 20, 0)
    runs = [{"workflowName": "Weekly totals", "conclusion": "failure", "createdAt": "2026-10-25T13:11:00Z", "databaseId": 1},
            {"workflowName": "Pregame planner", "conclusion": "success", "createdAt": "2026-10-25T16:40:00Z", "databaseId": 2},
            {"workflowName": "Settlement", "conclusion": "cancelled", "createdAt": "2026-10-25T14:30:00Z", "databaseId": 3},
            {"workflowName": "CI", "conclusion": "failure", "createdAt": "2026-10-25T14:30:00Z", "databaseId": 4},
            {"workflowName": "Collection monitor", "conclusion": "failure", "createdAt": "2026-10-23T12:15:00Z", "databaseId": 5},
            {"workflowName": "Pregame planner", "conclusion": "", "createdAt": "2026-10-25T19:55:00Z", "databaseId": 6}]
    assert [f["key"] for f in M.workflow_failures(now, runs)] == ["workflow-failure-1"]


def test_records_stale_alert_in_season_only_with_stable_title():
    head = u(2026, 10, 20, 18, 30)
    assert M.records_stale(u(2026, 10, 22, 12, 0), head) == []                 # 41.5 h
    a = M.records_stale(u(2026, 10, 22, 21, 0), head)
    b = M.records_stale(u(2026, 10, 23, 9, 0), head)
    assert len(a) == 1 and a[0]["title"] == b[0]["title"]                     # same incident, same title
    assert M.records_stale(u(2026, 7, 10, 12, 0), u(2026, 6, 1, 12, 0)) == []  # off season
    assert M.records_stale(u(2026, 10, 22, 21, 0), None) == []


class FakeGH:
    def __init__(self, titles=()):
        self.titles, self.created = list(titles), []

    def __call__(self, *args):
        if args[:2] == ("issue", "list"):
            return json.dumps([{"title": t} for t in self.titles])
        if args[:2] == ("issue", "create"):
            t = args[args.index("--title") + 1]
            self.created.append(t); self.titles.append(t)
        return ""


def test_issue_sync_never_duplicates_open_or_closed_issues():
    f1 = M._finding("halt-x", "Collection HALTED: x", "b")
    f2 = M._finding("missed-snapshot-sat_scheduled-20261024", "Missed scheduled snapshot sat", "b")
    gh = FakeGH(titles=[f1["title"]])            # already reported earlier (possibly closed by John)
    assert M.sync_issues([f1, f2, f2], gh=gh) == ["missed-snapshot-sat_scheduled-20261024"]
    assert M.sync_issues([f1, f2], gh=gh) == []   # next monitor run: nothing new
    assert gh.created == [f2["title"]]


def test_issue_sync_silent_when_healthy():
    gh = FakeGH()
    assert M.sync_issues([], gh=gh) == [] and gh.created == []


def test_run_checks_end_to_end_on_saved_records_layout(tmp_path):
    rec = tmp_path / "records"
    (rec / "data" / "odds" / "raw").mkdir(parents=True)
    for n in HEALTHY:
        (rec / "data" / "odds" / "raw" / n).write_text("{}")
    (rec / "data" / "predictions").mkdir(parents=True)
    out = M.run_checks(u(2026, 10, 28, 12, 0), rec, SCHED, [], u(2026, 10, 27, 14, 15), START)
    assert out == []
    (rec / "data" / "odds" / "raw" / "HALT_cost_anomaly_y.json").write_text("{}")
    assert [f["key"] for f in M.run_checks(u(2026, 10, 28, 12, 0), rec, SCHED, [], u(2026, 10, 27, 14, 15), START)] == \
        ["halt-HALT_cost_anomaly_y.json"]


def test_uncommitted_pull_is_flagged_through_the_failed_run():
    """A paid pull whose records commit failed (guard refusal or push failure) fails its job; the monitor
    turns that into an alert that names the records risk, once per run."""
    runs = [{"workflowName": "Pregame planner", "conclusion": "failure", "createdAt": "2026-10-25T16:41:00Z",
             "databaseId": 77, "url": "https://github.com/o/r/actions/runs/77"}]
    out = M.workflow_failures(u(2026, 10, 25, 18, 0), runs)
    assert len(out) == 1 and "records were not committed" in out[0]["body"] and "runs/77" in out[0]["body"]


def _claim(odds, ts, kind="thu_scheduled", run="555-1"):
    d = odds / "claims"; d.mkdir(parents=True, exist_ok=True)
    name = f"claim_{ts:%Y%m%dT%H%M%SZ}_{kind}_r{run}.json"
    (d / name).write_text(json.dumps({"claim_ts": ts.isoformat(), "kind": kind, "run_id": run}))
    return name


def test_unresolved_claim_flags_a_possibly_uncommitted_paid_capture(tmp_path):
    odds = tmp_path / "odds"; (odds / "raw").mkdir(parents=True)
    t = u(2026, 10, 22, 13, 11)
    name = _claim(odds, t)
    assert M.unresolved_claims(t + timedelta(minutes=15), odds, START) == []            # still inside TTL
    out = M.unresolved_claims(t + timedelta(hours=1), odds, START)
    assert [f["key"] for f in out] == [f"claim-unresolved-{name}"] and "Run 555" in out[0]["body"]
    assert M.unresolved_claims(t + timedelta(hours=5), odds, START)[0]["title"] == out[0]["title"]   # stable
    (odds / "raw" / raw(t + timedelta(seconds=20), "thu_scheduled")).write_text("{}")   # capture WITHOUT this claim id
    assert len(M.unresolved_claims(t + timedelta(hours=1), odds, START)) == 1           # does not resolve it
    (odds / "raw" / raw(t + timedelta(seconds=40), "thu_scheduled")).write_text(
        json.dumps({"snapshot_kind": "thu_scheduled", "claim_id": name[:-5]}))
    assert M.unresolved_claims(t + timedelta(hours=1), odds, START) == []               # its own capture landed
