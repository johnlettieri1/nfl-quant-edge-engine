"""Oct 25 2026 hotfix: kickoff-shift tolerance for pregame coverage, and a monitor that never fails its own
job because alert issues cannot be opened. Replays the real Oct 22 and Oct 25 cases. No network, no credits."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


from src.nfl.market import timing as T

UTC = timezone.utc


def u(*a):
    return datetime(*a, tzinfo=UTC)


def quotes_csv(path, ts, rows):
    path.write_text("snapshot_ts_utc,event_id,commence_time_utc_as_reported,book,total_line,over_price,under_price\n"
                    + "".join(f"{ts.isoformat()},{eid},{k.isoformat()},{b},44.5,-110,-110\n" for eid, k, b in rows))


def test_oct22_tnf_8_15_moved_to_8_20_no_second_pull(tmp_path):
    """Captured 7:45:48 PM when the kickoff was 8:15 PM (T-29.2). The Odds API then said 8:20 PM."""
    cap = u(2026, 10, 22, 23, 45, 48)
    quotes_csv(tmp_path / "q.csv", cap, [("ffebea88", u(2026, 10, 23, 0, 15), b) for b in ("dk", "fd", "mgm")])
    cov = T.coverage_from_quote_csvs([tmp_path / "q.csv"])
    names = [f"totals_{cap:%Y%m%dT%H%M%SZ}_pregame_window.json"]
    k_new = u(2026, 10, 23, 0, 20)
    d = T.plan_pregame(u(2026, 10, 23, 0, 5), [("ffebea88", k_new)], names, coverage=cov)
    assert not d.pull                                            # previously: pull=True (1 extra credit)


def test_oct25_one_pm_games_moved_to_1_05_no_second_pull(tmp_path):
    cap = u(2026, 10, 25, 16, 30, 55)                           # 12:30:55 PM, kickoffs reported as 1:00 PM
    rows = [(e, u(2026, 10, 25, 17, 0), "dk") for e in ("550ec5", "be5dd1", "f3992a")]
    quotes_csv(tmp_path / "q.csv", cap, rows)
    cov = T.coverage_from_quote_csvs([tmp_path / "q.csv"])
    names = [f"totals_{cap:%Y%m%dT%H%M%SZ}_pregame_window.json"]
    ev = [(e, u(2026, 10, 25, 17, 5)) for e, _, _ in rows]       # now reported as 1:05 PM
    assert not T.plan_pregame(u(2026, 10, 25, 16, 40, 30), ev, names, coverage=cov).pull


def test_shift_larger_than_5_minutes_is_a_new_window(tmp_path):
    cap = u(2026, 10, 25, 16, 30)
    quotes_csv(tmp_path / "q.csv", cap, [("g", u(2026, 10, 25, 17, 0), "dk")])
    cov = T.coverage_from_quote_csvs([tmp_path / "q.csv"])
    names = [f"totals_{cap:%Y%m%dT%H%M%SZ}_pregame_window.json"]
    d = T.plan_pregame(u(2026, 10, 25, 20, 0), [("g", u(2026, 10, 25, 20, 25))], names, coverage=cov)
    assert d.pull                                               # flexed 3 h 25 min later: needs its own capture


def test_capture_that_was_outside_its_own_window_still_does_not_cover(tmp_path):
    cap = u(2026, 10, 25, 16, 20)                               # T-40 for a 1:00 kickoff: not a pregame close
    quotes_csv(tmp_path / "q.csv", cap, [("g", u(2026, 10, 25, 17, 0), "dk")])
    cov = T.coverage_from_quote_csvs([tmp_path / "q.csv"])
    assert not T.covered_in_window(cov["g"], u(2026, 10, 25, 17, 5))
    assert not T.covered_in_window(cov["g"], u(2026, 10, 25, 17, 0))


def test_tolerance_never_counts_a_capture_at_or_after_the_current_kickoff():
    k_cap, k_now = u(2026, 10, 25, 17, 5), u(2026, 10, 25, 17, 0)   # moved EARLIER by 5 min
    entries = [(u(2026, 10, 25, 17, 0), 9, k_cap)]                   # captured exactly at the new kickoff
    assert not T.covered_in_window(entries, k_now)


def test_old_two_field_entries_still_work():
    k = u(2026, 10, 25, 17, 0)
    assert T.covered_in_window([(k - timedelta(minutes=20), 5)], k)
    assert not T.covered_in_window([(k - timedelta(minutes=35), 5)], k)


def test_monitor_issue_sync_failure_does_not_fail_the_job(tmp_path, monkeypatch, capsys):
    import argparse
    import run as runner
    from src.nfl.ops import monitor as M
    monkeypatch.setattr("src.nfl.data.ingest.fetch_schedules", lambda *a, **k: __import__("pandas").DataFrame(
        columns=["game_id", "season", "gameday", "gametime", "home_score", "away_score"]))
    monkeypatch.setattr(M, "run_checks", lambda *a, **k: [M._finding("halt-x", "Collection HALTED: x", "b")])

    def boom(findings):
        raise RuntimeError("issues are disabled for this repo")
    monkeypatch.setattr(M, "sync_issues", boom)
    out = tmp_path / "m.json"
    args = argparse.Namespace(records=str(tmp_path), runs_json=None, records_head_time=None, open_issues=True, out=str(out))
    assert runner.cmd_monitor(args) == 0
    res = json.loads(out.read_text())
    assert res["findings"] == ["halt-x"] and "disabled" in res["issue_sync_error"]
    assert "::error title=collection-alert issue sync::" in capsys.readouterr().out
