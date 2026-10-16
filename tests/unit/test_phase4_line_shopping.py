"""Phase 4: best-number rules, same-line price comparison, same-line de-vig, book scopes."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.nfl.data import odds_snapshot as O
from src.nfl.market import odds_math as M
from src.nfl.market.line_shopping import build_line_shopping, shop_metrics
from src.nfl.market.movement import apply_game_kickoff
from tests.unit._phase4_fixtures import PAYLOAD, SCHED, book, event

DT = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def q(rows):
    return pd.DataFrame(rows, columns=["book", "total_line", "over_price", "under_price"])


def test_best_over_lowest_line_then_best_price_at_that_line():
    m = shop_metrics(q([("a", 43.5, -110, -110), ("b", 43.0, -120, +100), ("c", 43.0, -105, -115), ("d", 44.0, +100, -120)]))
    assert (m["best_over_total"], m["best_over_price"], m["best_over_book"]) == (43.0, -105, "c")


def test_best_under_highest_line_then_best_price_at_that_line():
    m = shop_metrics(q([("a", 43.5, -110, -110), ("b", 44.0, -105, -115), ("c", 44.0, -120, +100), ("d", 43.0, -110, -110)]))
    assert (m["best_under_total"], m["best_under_price"], m["best_under_book"]) == (44.0, 100, "c")


def test_canonical_line_is_a_posted_number():
    assert M.canonical_line([42.0, 42.5, 43.0, 43.0]) == 43.0
    assert M.canonical_line([42.5, 43.0]) == 42.5           # tie: closest to median 42.75, then lower
    assert M.canonical_line([48.5, 49.5, 49.5, 48.5, 50.0, 49.0]) in (48.5, 49.5)
    m = shop_metrics(q([("a", 49.0, -110, -110), ("b", 49.5, -110, -110)]))
    assert m["median_total"] == 49.25 and m["canonical_total"] in (49.0, 49.5)


def test_prices_compared_only_at_the_same_number():
    m = shop_metrics(q([("a", 43.0, -110, -110), ("b", 43.5, -105, -115)]))
    # each line has a single book: no same-line comparison possible -> blank, not manufactured
    assert m["price_adv_cents_over"] is None and m["best_over_price_at_canonical"] is None
    assert m["consensus_over_price_at_canonical"] is None and m["consensus_over_prob_at_canonical"] is None
    m2 = shop_metrics(q([("a", 43.0, -110, -110), ("b", 43.0, -102, -118), ("c", 43.5, +120, -140)]))
    assert m2["canonical_total"] == 43.0 and m2["best_over_price_at_canonical"] == -102
    assert m2["price_adv_cents_over"] == 4.0   # median(-110,-102) = -106 -> -102 is 4 cents better


def test_consensus_devig_uses_same_line_books_only():
    m = shop_metrics(q([("a", 43.0, -110, -110), ("b", 43.0, -110, -110), ("c", 46.0, -300, +240)]))
    assert m["n_books_at_canonical"] == 2 and m["consensus_over_prob_at_canonical"] == 0.5


def test_cents_scale_is_continuous_across_even_money():
    assert M.cents(-110) == -110 and M.cents(100) == -100 and M.cents(-100) == -100 and M.cents(105) == -95
    assert M.median_price([-110, +110]) in (-100, 100)


def test_scopes_split_ny_actionable_from_all_observed():
    rows = O.parse_quotes(PAYLOAD, "s1", DT, "thu_scheduled", SCHED, "r", "h")
    ls = build_line_shopping(apply_game_kickoff(pd.DataFrame(rows)))
    r = ls.iloc[0]
    assert r.ny_n_books == 4                          # draftkings, fanduel, betmgm, espnbet
    assert r.all_n_books == 6                         # + pinnacle, bovada
    assert r.all_best_over_book == "bovada" and r.all_best_over_total == 41.5
    assert r.ny_best_over_book == "fanduel" and r.ny_best_over_total == 42.0   # offshore never in NY scope
    assert r.ref_book == "pinnacle" and r.ref_total == 42.5


def test_in_play_rows_excluded_from_shopping():
    rows = O.parse_quotes(PAYLOAD, "late", datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc), "manual", SCHED, "r", "h")
    assert build_line_shopping(apply_game_kickoff(pd.DataFrame(rows))).empty


def test_prediction_market_view_is_paired_and_fresh_only():
    from src.nfl.market.views import latest_market_view
    rows = O.parse_quotes(PAYLOAD, "20261016T120000Z_thu_scheduled", DT, "thu_scheduled", SCHED, "r", "h")
    ls = build_line_shopping(apply_game_kickoff(pd.DataFrame(rows)))
    v = latest_market_view(ls, "2026_07_SEA_DEN", 48, now=DT)
    assert v["market_source"] == "odds_snapshot:20261016T120000Z_thu_scheduled"
    assert v["market_total"] == 42.5 and v["market_total_basis"] == "median_all_observed_books"
    assert (v["best_over_line"], v["best_over_price"], v["best_over_book"]) == (41.5, -110, "bovada")
    assert (v["ny_best_over_line"], v["ny_best_over_price"], v["ny_best_over_book"]) == (42.0, -105, "fanduel")
    assert (v["ny_best_under_line"], v["ny_best_under_price"], v["ny_best_under_book"]) == (43.0, -105, "espnbet")
    assert "over_price" not in v and "under_price" not in v      # no unpaired price fields
    from datetime import timedelta
    assert latest_market_view(ls, "2026_07_SEA_DEN", 48, now=DT + timedelta(hours=49)) is None
