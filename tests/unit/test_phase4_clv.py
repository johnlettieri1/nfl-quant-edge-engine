"""Phase 4: line CLV sign, price CLV only at identical numbers, first/last observed rules."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.nfl.data import odds_snapshot as O
from src.nfl.market.clv import build_clv_events, line_clv, price_clv
from src.nfl.market.line_shopping import build_line_shopping
from src.nfl.market.movement import apply_game_kickoff, build_movement
from tests.unit._phase4_fixtures import SCHED, book, event

UTC = timezone.utc


def snaps(*specs):
    """specs: (datetime, kind, [books]) -> game-kickoff-applied quotes frame"""
    rows = []
    for dt, kind, books in specs:
        rows += O.parse_quotes([event(books)], f"{dt:%Y%m%dT%H%M%SZ}_{kind}", dt, kind, SCHED, "r", "h")
    return apply_game_kickoff(pd.DataFrame(rows))


S1 = (datetime(2026, 10, 13, 14, 10, tzinfo=UTC), "tue_scheduled",
      [book("draftkings", 44.0, -110, 44.0, -110), book("fanduel", 44.0, -105, 44.0, -115),
       book("betmgm", 44.5, -110, 44.5, -110), book("pinnacle", 44.0, -108, 44.0, -102)])
S2 = (datetime(2026, 10, 16, 13, 10, tzinfo=UTC), "thu_scheduled",
      [book("draftkings", 43.0, -110, 43.0, -110), book("fanduel", 43.0, -110, 43.0, -110),
       book("betmgm", 43.5, -110, 43.5, -110), book("pinnacle", 43.0, -105, 43.0, -105)])
R = (datetime(2026, 10, 18, 16, 40, tzinfo=UTC), "pregame_window",          # T-20, 5 books
     [book("draftkings", 42.5, -110, 42.5, -110), book("fanduel", 42.5, -115, 42.5, -105),
      book("betmgm", 43.0, -110, 43.0, -110), book("espnbet", 42.5, -108, 42.5, -112),
      book("pinnacle", 42.5, -104, 42.5, -106)])
POST = (datetime(2026, 10, 18, 17, 30, tzinfo=UTC), "manual",               # in-play: must never be the reference
        [book("draftkings", 30.5, -110, 30.5, -110)])


def test_line_clv_sign_for_over_and_under():
    assert line_clv("OVER", 44.5, 46.0) == 1.5     # took 44.5, market went to 46: beat it
    assert line_clv("UNDER", 44.5, 46.0) == -1.5
    assert line_clv("UNDER", 46.0, 44.5) == 1.5
    assert line_clv("OVER", 44.5, None) is None and line_clv("PASS", 44.5, 44.0) is None


def test_price_clv_direction():
    assert price_clv(-105, -115) > 0 and price_clv(-115, -105) < 0


def test_reference_is_last_snapshot_strictly_before_kickoff():
    ev = build_clv_events(snaps(S1, S2, R, POST))
    assert set(ev.ref_snapshot_id) == {"20261018T164000Z_pregame_window"}
    assert "20261018T173000Z_manual" not in set(ev.action_snapshot_id)
    assert bool(ev.ref_close_quality.iloc[0]) is True          # T-20 with 5 books


def test_close_quality_false_when_far_from_kickoff():
    ev = build_clv_events(snaps(S1, S2))
    assert set(ev.ref_snapshot_id) == {"20261016T131000Z_thu_scheduled"} and not ev.ref_close_quality.any()


def test_under_taken_early_beats_falling_market():
    ev = build_clv_events(snaps(S1, S2, R))
    u = ev[(ev.action_snapshot_id == "20261013T141000Z_tue_scheduled") & (ev.side == "UNDER") & (ev.variant == "best_available_ny")].iloc[0]
    assert (u.line_taken, u.book_taken) == (44.5, "betmgm") and u.ref_canonical_line == 42.5 and u.line_clv == 2.0
    o = ev[(ev.action_snapshot_id == "20261013T141000Z_tue_scheduled") & (ev.side == "OVER") & (ev.variant == "best_available_ny")].iloc[0]
    assert o.line_taken == 44.0 and o.line_clv == -1.5


def test_price_clv_blank_when_total_numbers_differ():
    ev = build_clv_events(snaps(S1, S2, R))
    e = ev[ev.action_snapshot_id == "20261013T141000Z_tue_scheduled"]
    # every number taken on Tuesday (44.0 / 44.5) is gone at R (42.5 / 43.0): all price CLV must be blank
    assert e[["price_clv_same_book", "price_clv_same_line_consensus", "fair_ev_vs_ref"]].isna().all().all()


def test_price_clv_present_only_at_identical_number():
    s_same = (datetime(2026, 10, 17, 13, 10, tzinfo=UTC), "sat_scheduled",
              [book("draftkings", 42.5, -102, 42.5, -118), book("fanduel", 42.5, -110, 42.5, -110), book("betmgm", 43.0, -110, 43.0, -110)])
    ev = build_clv_events(snaps(s_same, R))
    o = ev[(ev.side == "OVER") & (ev.variant == "best_available_ny")].iloc[0]
    assert (o.line_taken, o.book_taken, o.price_taken) == (42.5, "draftkings", -102)
    assert o.price_clv_same_book > 0                      # draftkings at R: 42.5 -110, we had -102
    assert o.price_clv_same_line_consensus is not None and o.fair_ev_vs_ref is not None   # 4 books at 42.5 at R


def test_first_observed_is_never_moved_by_later_snapshots():
    q1 = snaps(S1, S2)
    mv1 = build_movement(build_line_shopping(q1), q1)
    q2 = snaps(S1, S2, R, POST)
    mv2 = build_movement(build_line_shopping(q2), q2)
    assert mv1.first_observed_snapshot_id.iloc[0] == mv2.first_observed_snapshot_id.iloc[0] == "20261013T141000Z_tue_scheduled"
    assert mv2.first_observed_canonical.iloc[0] == 44.0
    assert mv2.last_observed_pregame_snapshot_id.iloc[0] == "20261018T164000Z_pregame_window"
    assert mv2.move_direction.iloc[0] == "down" and mv2.move_abs_pts.iloc[0] == 1.5
