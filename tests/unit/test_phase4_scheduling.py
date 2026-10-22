"""Scheduling recovery (approved Oct 2026): hybrid triggers, late-snapshot window, timing provenance.

Fixtures only. No network, no credits. The fake API below records every paid call so each test can
assert that duplicates, obsolete windows and post-kickoff attempts never reach the paid endpoint.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.nfl.data import odds_snapshot as O
from src.nfl.market import phase4_config as C
from src.nfl.market import timing as T
from tests.unit._phase4_fixtures import PAYLOAD, SCHED

UTC = timezone.utc


def u(*a):
    return datetime(*a, tzinfo=UTC)


def raw(dt, kind):
    return f"totals_{dt:%Y%m%dT%H%M%SZ}_{kind}.json"


# ---- late window vs the NEXT scheduled boundary ---------------------------------------------------

def test_late_window_is_capped_by_the_next_scheduled_target_not_only_18h():
    # sat 9:10 ET (EDT) -> 18 h = Sun 3:10 AM, earlier than sun_scheduled 9:10 -> 18 h cap applies
    sat = u(2026, 10, 24, 13, 10)
    assert T.late_window("sat_scheduled", sat) == sat + timedelta(hours=18)
    # sun 9:10 -> 18 h = Mon 3:10 AM, but sun_night 23:30 ET comes first -> next-target cap applies
    sun = u(2026, 10, 25, 13, 10)
    assert T.late_window("sun_scheduled", sun) == u(2026, 10, 26, 3, 30)
    # sun_night 23:30 -> 18 h = Mon 5:30 PM ET, before Tue 10:10 -> 18 h cap
    sn = u(2026, 10, 26, 3, 30)
    assert T.late_window("sun_night_scheduled", sn) == sn + timedelta(hours=18)
    # tue 10:10 -> 18 h = Wed 4:10 AM ET, before Thu 9:10
    tue = u(2026, 10, 27, 14, 10)
    assert T.late_window("tue_scheduled", tue) == tue + timedelta(hours=18)


@pytest.mark.parametrize("kind,target", [
    ("tue_scheduled", u(2026, 10, 27, 14, 10)), ("thu_scheduled", u(2026, 10, 29, 13, 10)),
    ("sat_scheduled", u(2026, 10, 31, 13, 10)), ("sun_scheduled", u(2026, 11, 1, 14, 10)),   # Nov 1: EST
    ("sun_night_scheduled", u(2026, 11, 2, 4, 30))])
def test_boundary_one_minute_before_and_at_window_end(kind, target):
    end = T.late_window(kind, target)
    assert end <= target + timedelta(minutes=C.SCHEDULE_LATE_MAX_MIN)
    assert end <= T.next_scheduled_target(target)
    before = T.scheduled_gate(end - timedelta(minutes=1), [])
    assert before.kind == kind and before.target == target and before.delayed
    at_end = T.scheduled_gate(end, [])
    assert at_end.kind != kind   # obsolete at the boundary (half-open window)


def test_windows_never_overlap_so_one_run_serves_at_most_one_kind():
    t = u(2026, 10, 20, 0, 0)
    while t < u(2026, 11, 17, 0, 0):
        live = [k for k in C.SCHEDULED_KINDS if (lambda tg: tg <= t < T.late_window(k, tg))(T._latest_target(k, t))]
        assert len(live) <= 1, (t, live)
        t += timedelta(minutes=20)


def test_incident_replay_oct_17_20_each_delayed_run_now_captures_with_honest_labels():
    cases = [(u(2026, 10, 17, 16, 22), "sat_scheduled", 192.0),       # Sat 12:22 PM ET
             (u(2026, 10, 18, 16, 30), "sun_scheduled", 200.0),       # Sun 12:30 PM ET
             (u(2026, 10, 19, 9, 13), "sun_night_scheduled", 343.0),  # Mon 5:13 AM ET
             (u(2026, 10, 20, 18, 26), "tue_scheduled", 256.0)]       # Tue 2:26 PM ET
    for now, kind, delay in cases:
        d = T.scheduled_gate(now, [])
        assert (d.kind, d.delay_minutes, d.delayed) == (kind, delay, True)


def test_obsolete_window_buys_nothing():
    # Wed 6:00 AM ET: tue window closed at Wed 4:10 AM; thu not yet due
    d = T.scheduled_gate(u(2026, 10, 28, 10, 0), [])
    assert d.kind is None and "obsolete" in d.reason and "tue_scheduled" in d.reason


# ---- duplicate triggers ---------------------------------------------------------------------------

def test_external_dispatch_plus_both_cron_twins_capture_once():
    tgt = u(2026, 10, 22, 13, 10)                              # thu 9:10 AM EDT
    ext = T.scheduled_gate(tgt + timedelta(seconds=20), [])    # external dispatch starts in ~20 s
    assert ext.kind == "thu_scheduled" and not ext.delayed
    have = [raw(tgt + timedelta(seconds=95), "thu_scheduled")]
    for late in (timedelta(hours=3), timedelta(hours=4), timedelta(hours=17)):   # both twins + very late
        assert T.scheduled_gate(tgt + late, have).kind is None
    # the dedupe is per (kind, target): next week's target is not blocked by this week's capture
    assert T.scheduled_gate(tgt + timedelta(days=7, seconds=30), have).kind == "thu_scheduled"


def test_capture_before_the_target_does_not_count_for_that_target():
    tgt = u(2026, 10, 22, 13, 10)
    early_manual = [raw(tgt - timedelta(minutes=30), "thu_scheduled")]
    assert T.scheduled_gate(tgt + timedelta(minutes=1), early_manual).kind == "thu_scheduled"


# ---- EST/EDT transition ---------------------------------------------------------------------------

def test_dst_fall_back_nov_1_2026():
    # Sun Nov 1 is the first EST day: sun_scheduled 9:10 AM EST = 14:10 UTC
    assert T._latest_target("sun_scheduled", u(2026, 11, 1, 15, 0)) == u(2026, 11, 1, 14, 10)
    assert T.scheduled_gate(u(2026, 11, 1, 13, 15), []).kind != "sun_scheduled"   # 8:15 AM EST: not yet
    d = T.scheduled_gate(u(2026, 11, 1, 14, 11), [])
    assert d.kind == "sun_scheduled" and d.delay_minutes == 1.0
    # sat Oct 31 (EDT) window crosses the 2 AM fall-back: 18 h from 13:10 UTC is 07:10 UTC, unaffected
    assert T.late_window("sat_scheduled", u(2026, 10, 31, 13, 10)) == u(2026, 11, 1, 7, 10)


def test_dst_spring_forward_mar_2027_and_wall_clock_targets():
    # Sun Mar 14 2027 becomes EDT at 2 AM; sun_night 11:30 PM EDT = 03:30 UTC Mon
    assert T._latest_target("sun_night_scheduled", u(2027, 3, 15, 4, 0)) == u(2027, 3, 15, 3, 30)
    # Sat Mar 13 is still EST: 9:10 AM EST = 14:10 UTC
    assert T._latest_target("sat_scheduled", u(2027, 3, 13, 15, 0)) == u(2027, 3, 13, 14, 10)


def test_forced_target_reports_real_delay_and_kind():
    d = T.forced_target("tue_scheduled", u(2026, 10, 20, 18, 26))
    assert d.kind == d.window_kind == "tue_scheduled" and d.target == u(2026, 10, 20, 14, 10) and d.delay_minutes == 256.0


# ---- pregame: weekday and international kickoffs, scheduled overlap -------------------------------

def test_international_930_game_waits_for_sun_scheduled_then_falls_back():
    k = u(2026, 10, 25, 13, 30)        # 9:30 AM EDT London kickoff; sun_scheduled due 9:10 = T-20
    ev = [("lon", k)]
    d = T.plan_pregame(k - timedelta(minutes=30), ev, [])
    assert not d.pull and "waiting for a scheduled snapshot" in d.reason
    d = T.plan_pregame(k - timedelta(minutes=15), ev, [])     # 9:15: still inside the 8-min grace
    assert not d.pull
    d = T.plan_pregame(k - timedelta(minutes=12), ev, [])     # 9:18: scheduled run never landed -> pull
    assert d.pull and d.targets[0]["event_id"] == "lon"
    # when the scheduled snapshot did land and quoted the game, it covers the game: no second pull
    cov = {"lon": [(k - timedelta(minutes=19), 8)]}
    names = [raw(k - timedelta(minutes=19), "sun_scheduled")]
    assert not T.plan_pregame(k - timedelta(minutes=12), ev, names, coverage=cov).pull


@pytest.mark.parametrize("kick", [
    u(2026, 10, 23, 0, 15),    # TNF 8:15 PM EDT (Thursday ET, Friday UTC)
    u(2026, 11, 13, 1, 15),    # TNF 8:15 PM EST
    u(2026, 11, 27, 20, 0),    # Black Friday 3:00 PM EST
    u(2026, 12, 26, 1, 15),    # Christmas night
    u(2026, 11, 15, 14, 30)])  # Sunday 9:30 AM EST international (sun_scheduled 9:10 = T-20)
def test_weekday_and_international_slots_pull_once_never_after_kickoff(kick):
    ev = [("e", kick)]
    d = T.plan_pregame(kick - timedelta(minutes=12), ev, [])
    assert d.pull
    names = [raw(kick - timedelta(minutes=12), "pregame_window")]
    cov = {"e": [(kick - timedelta(minutes=12), 7)]}
    for m in (11, 9, 6):
        assert not T.plan_pregame(kick - timedelta(minutes=m), ev, names, coverage=cov).pull
    for after in (0, 1, 30):
        assert not T.plan_pregame(kick + timedelta(minutes=after), ev, []).pull


def test_pregame_trigger_outside_any_window_never_pulls():
    k = u(2026, 10, 25, 17, 0)
    for m in (240, 60, 31, 4, 0, -10):
        assert not T.plan_pregame(k - timedelta(minutes=m), [("e", k)], []).pull


# ---- paid pull path: dedupe, obsolete, no backdating, timing provenance ---------------------------

class Fake:
    def __init__(self, fetched: datetime, remaining=400):
        self.fetched, self.remaining, self.calls = fetched, remaining, 0

    def balance(self):
        return {"remaining": self.remaining, "used": 100}

    def totals(self, books):
        self.calls += 1
        return {"endpoint": "/v4/sports/americanfootball_nfl/odds", "request_params": {"markets": "totals"},
                "http_status": 200, "retrieval_started_at": self.fetched.isoformat(), "fetched_at": self.fetched.isoformat(),
                "body": json.dumps(PAYLOAD).encode(), "x_requests_remaining": str(self.remaining - 1),
                "x_requests_used": "101", "x_requests_last": "1"}


def _fake(monkeypatch, fetched, remaining=400):
    f = Fake(fetched, remaining)
    monkeypatch.setattr(O, "fetch_balance", f.balance)
    monkeypatch.setattr(O, "fetch_totals_raw", f.totals)
    monkeypatch.setenv("ODDS_API_KEY", "SECRET-KEY-XYZ")
    return f


def test_delayed_capture_keeps_real_time_and_labels_target_delay_trigger(tmp_path, monkeypatch):
    tgt = u(2026, 10, 17, 13, 10)            # sat 9:10 AM EDT
    now = u(2026, 10, 17, 16, 22)            # GitHub cron started 3 h 12 min late
    f = _fake(monkeypatch, now)
    s = O.take_snapshot("sat_scheduled", "github_cron", SCHED, odds_dir=tmp_path, now=now,
                        scheduled_target=tgt.isoformat())
    assert f.calls == 1 and s["delayed"] is True and s["schedule_delay_minutes"] == 192.0
    rawf = tmp_path / "raw" / "totals_20261017T162200Z_sat_scheduled.json"   # named by REAL capture time
    env = json.loads(rawf.read_text())
    assert env["fetched_at"] == env["capture_ts_utc"] == now.isoformat()
    assert env["scheduled_target_utc"] == tgt.isoformat() and env["trigger"] == "github_cron" and env["delayed"] is True
    q = pd.read_csv(tmp_path / "quotes" / "quotes_20261017T162200Z_sat_scheduled.csv")
    assert set(q.snapshot_ts_utc) == {now.isoformat()} and set(q.scheduled_target_utc) == {tgt.isoformat()}
    assert set(q.schedule_delay_minutes) == {192.0} and set(q.delayed) == {True} and set(q.trigger) == {"github_cron"}
    # rebuilt-from-raw rows carry the same provenance
    rb = O.quotes_from_raw(tmp_path, SCHED)
    assert set(rb.scheduled_target_utc) == {tgt.isoformat()} and set(rb.trigger) == {"github_cron"}


def test_on_time_external_capture_is_not_labeled_delayed(tmp_path, monkeypatch):
    tgt = u(2026, 10, 22, 13, 10)
    now = tgt + timedelta(seconds=40)
    _fake(monkeypatch, now)
    s = O.take_snapshot("thu_scheduled", "external_dispatch", SCHED, odds_dir=tmp_path, now=now,
                        scheduled_target=tgt.isoformat())
    assert s["delayed"] is False and s["schedule_delay_minutes"] == 0.7 and s["trigger"] == "external_dispatch"


def test_capture_is_never_backdated_or_predated(tmp_path, monkeypatch):
    tgt = u(2026, 10, 22, 13, 10)
    f = _fake(monkeypatch, tgt - timedelta(minutes=5))
    with pytest.raises(ValueError, match="never backdated"):   # target in the future: refused before paying
        O.take_snapshot("thu_scheduled", "manual", SCHED, odds_dir=tmp_path, now=tgt - timedelta(minutes=5),
                        scheduled_target=tgt.isoformat())
    assert f.calls == 0 and not (tmp_path / "raw").exists()
    # the file name, snapshot_ts_utc and fetched_at always come from the real fetch time, never the target
    f.fetched = tgt + timedelta(hours=2)
    s = O.take_snapshot("thu_scheduled", "manual", SCHED, odds_dir=tmp_path, now=tgt + timedelta(hours=2),
                        scheduled_target=tgt.isoformat())
    assert s["snapshot_id"].startswith("20261022T151000Z") and "131000" not in s["snapshot_id"]


def test_prepay_recheck_blocks_duplicate_scheduled_pull(tmp_path, monkeypatch):
    tgt = u(2026, 10, 22, 13, 10)
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / raw(tgt + timedelta(minutes=1), "thu_scheduled")).write_text("{}")   # external won
    now = tgt + timedelta(hours=3)                                                           # cron twin queued
    f = _fake(monkeypatch, now)
    with pytest.raises(O.ScheduledNotNeeded, match="already captured"):
        O.take_snapshot("thu_scheduled", "github_cron", SCHED, odds_dir=tmp_path, now=now, scheduled_target=tgt.isoformat())
    assert f.calls == 0


def test_prepay_recheck_blocks_obsolete_window_unless_manual(tmp_path, monkeypatch):
    tgt = u(2026, 10, 20, 14, 10)                 # tue
    now = tgt + timedelta(hours=19)               # past the 18 h cap
    f = _fake(monkeypatch, now)
    with pytest.raises(O.ScheduledNotNeeded, match="obsolete"):
        O.take_snapshot("tue_scheduled", "external_dispatch", SCHED, odds_dir=tmp_path, now=now, scheduled_target=tgt.isoformat())
    assert f.calls == 0
    s = O.take_snapshot("tue_scheduled", "manual", SCHED, odds_dir=tmp_path, now=now, scheduled_target=tgt.isoformat())
    assert f.calls == 1 and s["schedule_delay_minutes"] == 1140.0 and s["delayed"] is True


def test_credit_reserve_cap_and_halt_still_apply_to_external_triggers(tmp_path, monkeypatch):
    tgt = u(2026, 10, 22, 13, 10)
    f = _fake(monkeypatch, tgt, remaining=59)
    with pytest.raises(O.BudgetError, match="reserve"):
        O.take_snapshot("thu_scheduled", "external_dispatch", SCHED, odds_dir=tmp_path, now=tgt, scheduled_target=tgt.isoformat())
    rawd = tmp_path / "raw"; rawd.mkdir()
    for i in range(30):
        (rawd / raw(u(2026, 10, 20, 12, 0) + timedelta(hours=i), "pregame_window")).write_text("{}")
    f.remaining = 400
    with pytest.raises(O.BudgetError, match="weekly cap"):
        O.take_snapshot("thu_scheduled", "external_dispatch", SCHED, odds_dir=tmp_path, now=tgt, scheduled_target=tgt.isoformat())
    for p in rawd.iterdir():
        p.unlink()
    (rawd / "HALT_cost_anomaly_x.json").write_text("{}")
    with pytest.raises(O.BudgetError, match="halted"):
        O.take_snapshot("thu_scheduled", "external_dispatch", SCHED, odds_dir=tmp_path, now=tgt, scheduled_target=tgt.isoformat())
    assert f.calls == 0


def test_unknown_trigger_rejected(tmp_path, monkeypatch):
    f = _fake(monkeypatch, u(2026, 10, 22, 13, 10))
    with pytest.raises(ValueError, match="trigger"):
        O.take_snapshot("manual", "cron-job.org", SCHED, odds_dir=tmp_path)
    assert f.calls == 0


def test_post_kickoff_rows_from_a_delayed_capture_are_not_pregame(tmp_path, monkeypatch):
    # sun 9:10 target, cron 5 h late at 2:10 PM ET: the 1:00 PM ET game is in progress
    tgt, now = u(2026, 10, 18, 13, 10), u(2026, 10, 18, 18, 10)
    _fake(monkeypatch, now)
    O.take_snapshot("sun_scheduled", "github_cron", SCHED, odds_dir=tmp_path, now=now, scheduled_target=tgt.isoformat())
    q = pd.read_csv(tmp_path / "quotes" / "quotes_20261018T181000Z_sun_scheduled.csv")
    assert (q.is_pregame == False).all() and (q.minutes_to_kickoff < 0).all()   # noqa: E712
