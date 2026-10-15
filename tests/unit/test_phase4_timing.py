"""Phase 4: America/New_York handling (EDT and EST), schedule gate, pregame planner."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.nfl.market import timing as T

UTC = timezone.utc


def test_nflverse_kickoff_converts_with_dst():
    assert T.nflverse_kickoff_utc("2026-10-18", "13:00") == datetime(2026, 10, 18, 17, 0, tzinfo=UTC)   # EDT
    assert T.nflverse_kickoff_utc("2026-11-08", "13:00") == datetime(2026, 11, 8, 18, 0, tzinfo=UTC)    # EST
    assert T.nflverse_kickoff_utc("2026-11-01", "13:00") == datetime(2026, 11, 1, 18, 0, tzinfo=UTC)    # DST ends 2 AM that day
    assert T.nflverse_kickoff_utc("2027-03-14", "13:00") == datetime(2027, 3, 14, 17, 0, tzinfo=UTC)    # DST starts


def test_et_date_crosses_midnight_correctly():
    assert str(T.et_date(datetime(2026, 10, 16, 0, 15, tzinfo=UTC))) == "2026-10-15"   # TNF 8:15 PM EDT
    assert str(T.et_date(datetime(2026, 11, 13, 1, 15, tzinfo=UTC))) == "2026-11-12"   # TNF 8:15 PM EST


def test_naive_timestamps_rejected():
    import pytest
    with pytest.raises(ValueError):
        T.parse_utc(datetime(2026, 10, 18, 17, 0))
    with pytest.raises(ValueError):
        T.parse_utc("2026-10-18T17:00:00")


def test_schedule_gate_keeps_only_the_right_dst_run():
    edt_run, est_cron_in_edt = datetime(2026, 10, 20, 14, 12, tzinfo=UTC), datetime(2026, 10, 20, 15, 12, tzinfo=UTC)
    assert T.scheduled_gate(edt_run, [])[0] == "tue_scheduled"
    assert T.scheduled_gate(est_cron_in_edt, ["totals_20261020T141200Z_tue_scheduled.json"])[0] is None   # dedupe
    edt_cron_in_est, est_run = datetime(2026, 11, 10, 14, 12, tzinfo=UTC), datetime(2026, 11, 10, 15, 12, tzinfo=UTC)
    assert T.scheduled_gate(edt_cron_in_est, [])[0] is None                      # 9:12 AM EST, before 10:10 target
    assert T.scheduled_gate(est_run, [])[0] == "tue_scheduled"
    # Sunday 11:30 PM ET crosses into Monday UTC in both seasons
    assert T.scheduled_gate(datetime(2026, 10, 19, 3, 31, tzinfo=UTC), [])[0] == "sun_night_scheduled"
    assert T.scheduled_gate(datetime(2026, 11, 9, 4, 31, tzinfo=UTC), [])[0] == "sun_night_scheduled"
    assert T.scheduled_gate(datetime(2026, 11, 9, 3, 31, tzinfo=UTC), [])[0] is None   # 10:31 PM EST: too early


def test_schedule_gate_falls_back_when_first_run_dropped():
    # EDT: first cron dropped by GitHub; the EST-hour cron (one hour later) still captures it
    assert T.scheduled_gate(datetime(2026, 10, 18, 14, 12, tzinfo=UTC), [])[0] == "sun_scheduled"


def test_close_quality_rule():
    assert T.close_quality(20, 5) and T.close_quality(1, 9) and T.close_quality(30, 5)
    assert not T.close_quality(31, 9) and not T.close_quality(20, 4) and not T.close_quality(0, 9) and not T.close_quality(-5, 9)


def names(*dts, kind="pregame_window"):
    return [f"totals_{d:%Y%m%dT%H%M%SZ}_{kind}.json" for d in dts]


K = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)


def test_planner_pulls_once_in_window_and_never_after_kickoff():
    assert not T.plan_pregame(K - timedelta(minutes=34), [K], []).pull          # too early, wait
    d = T.plan_pregame(K - timedelta(minutes=22), [K], [])
    assert d.pull and d.target_kickoffs == [K.isoformat()]
    assert not T.plan_pregame(K - timedelta(minutes=12), [K], names(K - timedelta(minutes=22))).pull   # covered
    assert not T.plan_pregame(K + timedelta(minutes=1), [K], []).pull           # after kickoff: never
    assert not T.plan_pregame(K - timedelta(minutes=3), [K], []).pull           # inside T-5: never


def test_planner_one_late_retry_max_two_attempts():
    late_first = names(K - timedelta(minutes=34))                                # landed before the window
    assert T.plan_pregame(K - timedelta(minutes=12), [K], late_first).pull       # retry inside window
    assert T.plan_pregame(K - timedelta(minutes=7), [K], []).pull                # single late attempt allowed
    assert not T.plan_pregame(K - timedelta(minutes=7), [K], names(K - timedelta(minutes=33))).pull
    two = names(K - timedelta(minutes=38), K - timedelta(minutes=33))
    assert not T.plan_pregame(K - timedelta(minutes=12), [K], two).pull          # max 2 attempts per slot


def test_planner_any_snapshot_kind_counts_as_coverage_and_slots_merge():
    k2 = K + timedelta(minutes=5)
    sun = names(K - timedelta(minutes=20), kind="sun_scheduled")
    assert not T.plan_pregame(K - timedelta(minutes=12), [K], sun).pull
    d = T.plan_pregame(K - timedelta(minutes=20), [K, k2], [])
    assert d.pull and len(d.target_kickoffs) == 2                                # one pull covers both


def test_planner_stops_on_halt_file():
    assert not T.plan_pregame(K - timedelta(minutes=20), [K], ["HALT_cost_anomaly_20261018T120000Z.json"]).pull


def test_planner_respects_weekly_cap():
    assert not T.plan_pregame(K - timedelta(minutes=20), [K], [], credits_used_this_week=30).pull


def test_week_starts_tuesday_midnight_eastern():
    assert T.nfl_week_start(datetime(2026, 10, 18, 17, 0, tzinfo=UTC)) == datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
    assert T.nfl_week_start(datetime(2026, 11, 15, 17, 0, tzinfo=UTC)) == datetime(2026, 11, 10, 5, 0, tzinfo=UTC)


def test_raw_name_parsing():
    assert T.parse_raw_name("totals_20261015T180351Z_current.json")[1] == "current"
    assert T.parse_raw_name("totals_20261018T164000Z_pregame_window-dup1.json")[1] == "pregame_window"
    assert T.parse_raw_name("forecast_x.json") is None
