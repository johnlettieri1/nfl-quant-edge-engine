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
    # 10:31 PM EST Sunday: too early for sun_night; under the late-snapshot policy this is a DELAYED
    # sun_scheduled capture if (and only if) the 9:10 AM one never landed
    d = T.scheduled_gate(datetime(2026, 11, 9, 3, 31, tzinfo=UTC), [])
    assert d.kind == "sun_scheduled" and d.delayed and d.delay_minutes == 801.0
    assert T.scheduled_gate(datetime(2026, 11, 9, 3, 31, tzinfo=UTC), ["totals_20261108T141200Z_sun_scheduled.json"])[0] is None


def test_schedule_gate_falls_back_when_first_run_dropped():
    # EDT: first cron dropped by GitHub; the EST-hour cron (one hour later) still captures it
    assert T.scheduled_gate(datetime(2026, 10, 18, 14, 12, tzinfo=UTC), [])[0] == "sun_scheduled"


def test_close_quality_rule():
    R = T.KICKOFF_RESOLVED
    assert T.close_quality(20, 5, R) and T.close_quality(1, 9, R) and T.close_quality(30, 5, R)
    assert not T.close_quality(31, 9, R) and not T.close_quality(20, 4, R) and not T.close_quality(0, 9, R)
    assert not T.close_quality(-5, 9, R)
    assert not T.close_quality(20, 9, T.KICKOFF_UNRESOLVED) and not T.close_quality(20, 9, None)   # unresolved kickoff


def names(*dts, kind="pregame_window"):
    return [f"totals_{d:%Y%m%dT%H%M%SZ}_{kind}.json" for d in dts]


K = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
E = [("evtA", K)]


def cov(*items):
    """items: (event_id, snapshot_dt, n_books)"""
    out = {}
    for eid, ts, n in items:
        out.setdefault(eid, []).append((ts, n))
    return out


def test_planner_pulls_once_in_window_and_never_after_kickoff():
    assert not T.plan_pregame(K - timedelta(minutes=34), E, []).pull          # too early, wait
    d = T.plan_pregame(K - timedelta(minutes=22), E, [])
    assert d.pull and d.targets == [{"event_id": "evtA", "kickoff": K.isoformat()}]
    t = K - timedelta(minutes=22)
    assert not T.plan_pregame(K - timedelta(minutes=12), E, names(t), coverage=cov(("evtA", t, 9))).pull   # covered
    assert not T.plan_pregame(K + timedelta(minutes=1), E, []).pull           # after kickoff: never
    assert not T.plan_pregame(K, E, []).pull                                  # at kickoff: never
    assert not T.plan_pregame(K - timedelta(minutes=3), E, []).pull           # inside T-5: never


def test_snapshot_exists_but_target_game_not_quoted_is_not_covered():
    t = K - timedelta(minutes=22)
    other = cov(("evtB", t, 10))                    # the T-22 snapshot quoted a different game only
    d = T.plan_pregame(K - timedelta(minutes=12), E, names(t), coverage=other)
    assert d.pull and [x["event_id"] for x in d.targets] == ["evtA"]   # one more attempt allowed (cap 2)


def test_multiple_games_kicking_off_together_are_tracked_separately():
    e = [("evtA", K), ("evtB", K), ("evtC", K + timedelta(minutes=5))]
    t = K - timedelta(minutes=22)
    c = cov(("evtA", t, 10), ("evtB", t, 10))       # evtC missing from that snapshot
    d = T.plan_pregame(K - timedelta(minutes=12), e, names(t), coverage=c)
    assert d.pull and [x["event_id"] for x in d.targets] == ["evtC"]
    c2 = cov(("evtA", t, 10), ("evtB", t, 10), ("evtC", t, 8))
    assert not T.plan_pregame(K - timedelta(minutes=12), e, names(t), coverage=c2).pull
    d3 = T.plan_pregame(K - timedelta(minutes=20), e, [])
    assert d3.pull and len(d3.targets) == 3 and d3.target_kickoffs == sorted({K.isoformat(), (K + timedelta(minutes=5)).isoformat()})


def test_game_quoted_by_some_but_not_all_books_counts_as_covered():
    t = K - timedelta(minutes=22)
    assert not T.plan_pregame(K - timedelta(minutes=12), E, names(t), coverage=cov(("evtA", t, 3))).pull
    # (close_quality separately requires 5 books, see test_close_quality_rule)


def test_valid_coverage_prevents_duplicate_pulls_on_every_later_tick():
    t = K - timedelta(minutes=25)
    c = cov(("evtA", t, 10))
    for m in (21, 15, 11, 8, 6):
        assert not T.plan_pregame(K - timedelta(minutes=m), E, names(t), coverage=c).pull


def test_coverage_outside_the_window_does_not_count():
    early = K - timedelta(minutes=34)                 # quoted, but before the T-30 window
    d = T.plan_pregame(K - timedelta(minutes=20), E, names(early), coverage=cov(("evtA", early, 10)))
    assert d.pull


def test_planner_one_late_retry_max_two_attempts():
    assert T.plan_pregame(K - timedelta(minutes=12), E, names(K - timedelta(minutes=34))).pull
    assert T.plan_pregame(K - timedelta(minutes=7), E, []).pull                # single late attempt allowed
    assert not T.plan_pregame(K - timedelta(minutes=7), E, names(K - timedelta(minutes=33))).pull
    two = names(K - timedelta(minutes=38), K - timedelta(minutes=33))
    assert not T.plan_pregame(K - timedelta(minutes=12), E, two).pull          # max 2 attempts per game


def test_planner_stops_on_halt_file():
    assert not T.plan_pregame(K - timedelta(minutes=20), E, ["HALT_cost_anomaly_20261018T120000Z.json"]).pull


def test_planner_respects_weekly_cap():
    assert not T.plan_pregame(K - timedelta(minutes=20), E, [], credits_used_this_week=30).pull


def test_coverage_from_quote_csvs_counts_valid_rows_per_game(tmp_path):
    p = tmp_path / "quotes_x.csv"
    p.write_text("snapshot_ts_utc,event_id,book,total_line,over_price,under_price\n"
                 "2026-10-18T16:40:00+00:00,evtA,draftkings,42.5,-110,-110\n"
                 "2026-10-18T16:40:00+00:00,evtA,fanduel,42.5,-110,-110\n"
                 "2026-10-18T16:40:00+00:00,evtB,draftkings,,,\n")      # unpaired/blank row ignored
    c = T.coverage_from_quote_csvs([p])
    assert c == {"evtA": [(datetime(2026, 10, 18, 16, 40, tzinfo=UTC), 2, None)]}


def test_week_starts_tuesday_midnight_eastern():
    assert T.nfl_week_start(datetime(2026, 10, 18, 17, 0, tzinfo=UTC)) == datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
    assert T.nfl_week_start(datetime(2026, 11, 15, 17, 0, tzinfo=UTC)) == datetime(2026, 11, 10, 5, 0, tzinfo=UTC)


def test_raw_name_parsing():
    assert T.parse_raw_name("totals_20261015T180351Z_current.json")[1] == "current"
    assert T.parse_raw_name("totals_20261018T164000Z_pregame_window-dup1.json")[1] == "pregame_window"
    assert T.parse_raw_name("forecast_x.json") is None
