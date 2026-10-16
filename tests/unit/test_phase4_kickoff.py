"""Phase 4 kickoff policy: flexed, postponed, conflicting and DST cases (fixtures only, no API calls)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from src.nfl.data import odds_snapshot as O
from src.nfl.market import timing as T
from src.nfl.market.clv import build_clv_events
from src.nfl.market.line_shopping import build_line_shopping
from src.nfl.market.movement import apply_game_kickoff, build_movement
from tests.unit._phase4_fixtures import SCHED, book, event

UTC = timezone.utc
BOOKS = [book(b, 43.0, -110, 43.0, -110) for b in ("draftkings", "fanduel", "betmgm", "betrivers", "espnbet")]


def sched_with(gameday, gametime):
    s = SCHED.copy()
    s.loc[s.game_id == "2026_07_SEA_DEN", ["gameday", "gametime"]] = [gameday, gametime]
    return s


def quotes(specs, sched):
    """specs: (snapshot_dt, kind, commence_iso_reported_by_that_snapshot)"""
    rows = []
    for dt, kind, commence in specs:
        rows += O.parse_quotes([event(BOOKS, commence=commence)], f"{dt:%Y%m%dT%H%M%SZ}_{kind}", dt, kind, sched, "r", "h")
    return apply_game_kickoff(pd.DataFrame(rows), sched)


def test_policy_resolution_rules():
    a = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
    r = T.resolve_game_kickoff(a, a + timedelta(minutes=3))
    assert (r["source"], r["status"], r["kickoff"]) == ("agreed", "resolved", a)
    r = T.resolve_game_kickoff(a + timedelta(hours=3), a)
    assert (r["source"], r["status"], r["kickoff"], r["conflict_minutes"]) == ("conflict", "unresolved", a, 180.0)
    assert T.resolve_game_kickoff(a, None)["status"] == "unresolved" and T.resolve_game_kickoff(a, None)["source"] == "odds_api_only"
    assert T.resolve_game_kickoff(None, a)["source"] == "nflverse_only"


def test_flexed_earlier_game_drops_post_kickoff_snapshots():
    # SNF flexed to 4:25 PM ET; both sources now agree on 20:25 UTC
    s = sched_with("2026-10-18", "16:25")
    q = quotes([(datetime(2026, 10, 18, 13, 10, tzinfo=UTC), "sun_scheduled", "2026-10-19T00:20:00Z"),   # before flex news
                (datetime(2026, 10, 18, 20, 5, tzinfo=UTC), "pregame_window", "2026-10-18T20:25:00Z"),
                (datetime(2026, 10, 18, 21, 0, tzinfo=UTC), "manual", "2026-10-18T20:25:00Z")], s)       # after new kickoff
    g = q.drop_duplicates("snapshot_id").set_index("snapshot_kind")
    assert g.loc["pregame_window", "kickoff_game_status"] == "resolved" and bool(g.loc["pregame_window", "kickoff_changed"])
    assert bool(g.loc["sun_scheduled", "is_pregame_game"]) and bool(g.loc["pregame_window", "is_pregame_game"])
    assert not bool(g.loc["manual", "is_pregame_game"])
    mv = build_movement(build_line_shopping(q), q)
    assert mv.last_observed_pregame_kind.iloc[0] == "pregame_window" and bool(mv.close_quality.iloc[0])


def test_verified_postponement_keeps_valid_pregame_observations():
    # 1:00 PM ET game postponed to 8:00 PM ET; both sources verified the new time
    s = sched_with("2026-10-18", "20:00")
    q = quotes([(datetime(2026, 10, 18, 13, 10, tzinfo=UTC), "sun_scheduled", "2026-10-18T17:00:00Z"),   # before postponement
                (datetime(2026, 10, 18, 17, 30, tzinfo=UTC), "manual", "2026-10-18T17:00:00Z"),          # after OLD start, own view = started
                (datetime(2026, 10, 18, 22, 0, tzinfo=UTC), "thu_scheduled", "2026-10-19T00:00:00Z"),    # new time posted
                (datetime(2026, 10, 18, 23, 40, tzinfo=UTC), "pregame_window", "2026-10-19T00:00:00Z")], s)
    g = q.drop_duplicates("snapshot_id").set_index("snapshot_kind")
    assert (g.loc["pregame_window", "kickoff_game_source"], g.loc["pregame_window", "kickoff_game_status"]) == ("agreed", "resolved")
    assert bool(g.loc["thu_scheduled", "is_pregame_game"]) and bool(g.loc["pregame_window", "is_pregame_game"])  # kept
    assert bool(g.loc["sun_scheduled", "is_pregame_game"])
    assert not bool(g.loc["manual", "is_pregame_game"])        # it saw the game as already started: never pregame
    ev = build_clv_events(q)
    assert set(ev.ref_snapshot_id) == {"20261018T234000Z_pregame_window"} and ev.ref_close_quality.all()


def test_unverified_postponement_is_conservative_and_refuses_close_quality():
    # Odds API shows the new 8:00 PM ET time but nflverse still says 1:00 PM ET: conflict
    s = sched_with("2026-10-18", "13:00")
    q = quotes([(datetime(2026, 10, 18, 13, 10, tzinfo=UTC), "sun_scheduled", "2026-10-18T17:00:00Z"),
                (datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window", "2026-10-19T00:00:00Z"),
                (datetime(2026, 10, 18, 23, 40, tzinfo=UTC), "manual", "2026-10-19T00:00:00Z")], s)
    g = q.drop_duplicates("snapshot_id").set_index("snapshot_kind")
    assert (g.loc["manual", "kickoff_game_source"], g.loc["manual", "kickoff_game_status"]) == ("conflict", "unresolved")
    assert g.loc["manual", "kickoff_game_conflict_minutes"] == 420.0
    assert not bool(g.loc["manual", "is_pregame_game"])        # after the EARLIER candidate kickoff: never pregame
    assert bool(g.loc["pregame_window", "is_pregame_game"])    # still before both candidate times
    mv = build_movement(build_line_shopping(q), q)
    assert mv.kickoff_status.iloc[0] == "unresolved" and not bool(mv.close_quality.iloc[0])   # T-20 but refused
    assert not build_clv_events(q).ref_close_quality.any()


def test_est_edt_change_resolves_without_false_conflicts():
    for gameday, commence in (("2026-10-25", "2026-10-25T17:00:00Z"),    # EDT: 1:00 PM ET = 17:00 UTC
                              ("2026-11-08", "2026-11-08T18:00:00Z")):   # EST: 1:00 PM ET = 18:00 UTC
        s = sched_with(gameday, "13:00")
        dt = T.parse_utc(commence) - timedelta(minutes=20)
        q = quotes([(dt - timedelta(days=2), "thu_scheduled", commence), (dt, "pregame_window", commence)], s)
        assert set(q.kickoff_game_source) == {"agreed"} and set(q.kickoff_game_status) == {"resolved"}
        assert set(q.minutes_to_kickoff_game.round()) >= {20.0}


def test_unmatched_game_is_single_source_and_never_close_quality():
    sched = SCHED[SCHED.game_id != "2026_07_SEA_DEN"]
    q = quotes([(datetime(2026, 10, 16, 13, 10, tzinfo=UTC), "thu_scheduled", "2026-10-18T17:00:00Z"),
                (datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window", "2026-10-18T17:00:00Z")], sched)
    assert set(q.kickoff_game_source) == {"odds_api_only"}
    mv = build_movement(build_line_shopping(q), q)
    assert not bool(mv.close_quality.iloc[0])


def test_quote_rows_record_kickoff_source_and_conflict():
    rows = O.parse_quotes([event(BOOKS, commence="2026-10-18T20:00:00Z")], "s", datetime(2026, 10, 16, tzinfo=UTC),
                          "thu_scheduled", SCHED, "r", "h")
    r = rows[0]
    assert r["kickoff_nflverse_utc"] == "2026-10-18T17:00:00+00:00" and r["kickoff_source"] == "conflict"
    assert r["kickoff_status"] == "unresolved" and r["kickoff_conflict_minutes"] == 180.0
    assert r["kickoff_utc_resolved"] == "2026-10-18T17:00:00+00:00"
