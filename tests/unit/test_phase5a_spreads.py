"""Phase 5A: paired spread collection in the same pull as totals (fixtures only, no network, no credits)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.nfl.data import odds_api as A
from src.nfl.data import odds_snapshot as O
from src.nfl.market import timing as T
from src.nfl.market.spread_tables import build_spread_line_shopping, build_spread_movement
from src.nfl.ops.records_guard import evaluate
from tests.unit._phase4_fixtures import PAYLOAD, SCHED, book, event

UTC = timezone.utc
SNAP = datetime(2026, 10, 18, 16, 40, tzinfo=UTC)     # T-20 for the 1:00 PM ET fixture game
HOME, AWAY = "Denver Broncos", "Seattle Seahawks"
# the shared totals fixture includes one spreads-only book; a totals-only (pre-5A) response never has spreads
TOTALS_ONLY = [{**PAYLOAD[0], "bookmakers": [b for b in PAYLOAD[0]["bookmakers"] if b["key"] != "spreads_only"]}]


def spread_book(key, home_pt, home_px, away_pt, away_px, home=HOME, away=AWAY):
    return {"key": key, "markets": [{"key": "spreads", "last_update": "2026-10-18T16:38:00Z", "outcomes": [
        {"name": home, "price": home_px, "point": home_pt}, {"name": away, "price": away_px, "point": away_pt}]}]}


def both(key, total, home_pt, hp=-110, ap=-110):
    b = book(key, total, -110, total, -110)
    b["markets"] += spread_book(key, home_pt, hp, -home_pt, ap)["markets"]
    return b


def payload(books):
    return [event(books)]


def parse(books, dt=SNAP):
    return O.parse_spread_quotes(payload(books), "sid", dt, "pregame_window", SCHED, "raw.json", "abc")


# ---- parsing --------------------------------------------------------------------------------------

def test_spread_number_and_both_prices_stay_paired_to_one_book():
    rows = parse([spread_book("draftkings", -3.0, -115, 3.0, -105), spread_book("fanduel", -3.5, 100, 3.5, -120)])
    assert [(r["book"], r["home_spread"], r["home_price"], r["away_spread"], r["away_price"]) for r in rows] == [
        ("draftkings", -3.0, -115, 3.0, -105), ("fanduel", -3.5, 100, 3.5, -120)]
    assert all(r["game_id"] == "2026_07_SEA_DEN" and r["home_team"] == "DEN" and r["is_pregame"] for r in rows)


@pytest.mark.parametrize("bad", [
    spread_book("x", -3.0, -110, 2.5, -110),                                  # numbers do not mirror
    {"key": "x", "markets": [{"key": "spreads", "outcomes": [{"name": HOME, "price": -110, "point": -3.0}]}]},  # one side
    spread_book("x", -3.0, -110, 3.0, -110, away="Seattle Sea Lions"),        # unmapped team name
    {"key": "x", "markets": [{"key": "spreads", "outcomes": [{"name": HOME, "price": -110}, {"name": AWAY, "price": -110}]}]},
])
def test_invalid_spread_quotes_are_skipped_never_patched(bad):
    assert parse([bad]) == []


def test_pick_em_zero_has_no_negative_zero():
    r = parse([spread_book("dk", 0.0, -110, -0.0, -110)])[0]
    assert str(r["home_spread"]) == "0.0" and str(r["away_spread"]) == "0.0"


def test_totals_rows_unchanged_when_the_response_also_has_spreads():
    totals_only = O.parse_quotes(TOTALS_ONLY, "sid", SNAP, "manual", SCHED, "raw.json", "abc")
    mixed = json.loads(json.dumps(TOTALS_ONLY))
    for b in mixed[0]["bookmakers"]:
        b["markets"] = b["markets"] + spread_book(b["key"], -3.0, -110, 3.0, -110)["markets"]
    assert O.parse_quotes(mixed, "sid", SNAP, "manual", SCHED, "raw.json", "abc") == totals_only
    assert len(O.parse_spread_quotes(mixed, "sid", SNAP, "manual", SCHED, "raw.json", "abc")) == len(mixed[0]["bookmakers"])


def test_request_asks_for_totals_and_spreads_in_one_call(monkeypatch):
    seen = {}

    class R:
        status_code, content, headers = 200, b"[]", {"x-requests-last": "2"}
    monkeypatch.setattr(A, "_request", lambda path, params, timeout=30: seen.update(params) or R())
    A.fetch_totals_raw(["draftkings"])
    assert seen["markets"] == "totals,spreads" and seen["bookmakers"] == "draftkings"


# ---- paid path: files, cost guard, weekly credits -------------------------------------------------

class Fake:
    def __init__(self, body, last="2", remaining=400):
        self.body, self.last, self.remaining, self.calls = body, last, remaining, 0

    def balance(self):
        return {"remaining": self.remaining, "used": 1}

    def totals(self, books):
        self.calls += 1
        return {"endpoint": "/v4", "request_params": {"markets": "totals,spreads"}, "http_status": 200,
                "retrieval_started_at": SNAP.isoformat(), "fetched_at": SNAP.isoformat(), "body": self.body,
                "x_requests_remaining": str(self.remaining - int(self.last)), "x_requests_used": "3",
                "x_requests_last": self.last}


@pytest.fixture
def api(monkeypatch):
    f = Fake(json.dumps(payload([both("draftkings", 44.5, -3.0), both("fanduel", 44.0, -3.5),
                                    both("pinnacle", 44.5, -3.0)])).encode())
    monkeypatch.setattr(O, "fetch_balance", f.balance)
    monkeypatch.setattr(O, "fetch_totals_raw", f.totals)
    return f


def test_snapshot_writes_totals_and_spread_files_and_rebuild_matches(tmp_path, api):
    s = O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP)
    assert s["quote_rows"] == 3 and s["spread_quote_rows"] == 3 and api.calls == 1
    written = O.load_spread_quotes(tmp_path)
    assert list(written.columns) == O.SPREAD_FIELDS and len(written) == 3
    rebuilt = O.spread_quotes_from_raw(tmp_path, SCHED)
    assert rebuilt[["book", "home_spread", "home_price", "away_price"]].values.tolist() == \
        written[["book", "home_spread", "home_price", "away_price"]].values.tolist()
    assert not O.halt_files(tmp_path)                                    # 2 credits is the expected cost


def test_more_than_two_credits_halts(tmp_path, api):
    api.last = "3"
    with pytest.raises(O.CostAnomalyError):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP)
    assert O.halt_files(tmp_path) and len(O.load_spread_quotes(tmp_path)) == 3   # data kept


def test_pre_phase5a_snapshots_contribute_no_spread_rows(tmp_path):
    raw = tmp_path / "raw"; raw.mkdir()
    body = json.dumps(TOTALS_ONLY)
    (raw / "totals_20261016T131000Z_thu_scheduled.json").write_text(json.dumps({
        "schema_version": "odds_raw_v2", "snapshot_kind": "thu_scheduled", "fetched_at": "2026-10-16T13:10:00+00:00",
        "http_status": 200, "response_sha256": __import__("hashlib").sha256(body.encode()).hexdigest(),
        "response_body": body}))
    assert O.spread_quotes_from_raw(tmp_path, SCHED).empty and len(O.quotes_from_raw(tmp_path, SCHED)) == 6


def _env(raw, dt, kind, last):
    (raw / f"totals_{dt:%Y%m%dT%H%M%SZ}_{kind}.json").write_text(json.dumps({"x_requests_last": last}))


def test_weekly_cap_counts_real_credits_old_one_credit_pulls_count_once(tmp_path, api):
    raw = tmp_path / "raw"; raw.mkdir()
    start = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)                      # Tue Oct 13 (week of the fixture)
    for i in range(30):
        _env(raw, start + timedelta(hours=i), "pregame_window", "1")       # 30 old totals-only pulls = 30 credits
    names = sorted(p.name for p in raw.iterdir())
    assert T.credits_this_week(SNAP, names, tmp_path) == 30
    assert T.credits_this_week(SNAP, names) == 60                          # without envelopes: conservative 2 each
    O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP)
    assert api.calls == 1


@pytest.mark.parametrize("n,allowed", [(29, True), (30, False)])
def test_weekly_cap_is_60_credits_at_2_per_pull(tmp_path, api, n, allowed):
    raw = tmp_path / "raw"; raw.mkdir()
    start = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    for i in range(n):
        _env(raw, start + timedelta(hours=i), "pregame_window", "2")
    if allowed:
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP)       # 58 + 2 = 60
        assert api.calls == 1
    else:
        with pytest.raises(O.BudgetError, match="weekly cap"):
            O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=SNAP)   # 60 + 2 > 60
        assert api.calls == 0


def test_records_guard_accepts_new_spread_files_and_protects_them():
    path = "data/odds/spread_quotes/spread_quotes_20261018T164000Z_pregame_window.csv"
    assert evaluate([("A", path)], lambda p: b"", lambda p: b"a,b\n1,2\n") == []
    assert evaluate([("M", path)], lambda p: b"a,b\n1,2\n", lambda p: b"a,b\n9,9\n") != []
    assert evaluate([("D", path)], lambda p: b"x", lambda p: b"") != []


# ---- derived tables -------------------------------------------------------------------------------

def _q(dt, books):
    return pd.DataFrame(O.parse_spread_quotes(payload(books), f"{dt:%Y%m%dT%H%M%SZ}_x", dt, "pregame_window", SCHED,
                                              "raw.json", "abc"))


def test_line_shopping_best_numbers_and_key_number_three():
    q = _q(SNAP, [spread_book("draftkings", -3.0, -110, 3.0, -110), spread_book("fanduel", -3.0, -105, 3.0, -115),
                  spread_book("betmgm", -2.5, -120, 2.5, 100), spread_book("betonlineag", -3.5, 105, 3.5, -125),
                  spread_book("pinnacle", -3.0, -108, 3.0, -102)])
    r = build_spread_line_shopping(q).iloc[0]
    assert r.all_canonical_home_spread == -3.0
    assert (r.all_best_home_spread, r.all_best_home_book) == (-2.5, "betmgm")       # favorite laying fewer points
    assert (r.all_best_away_spread, r.all_best_away_book) == (3.5, "betonlineag")   # dog getting more points
    assert bool(r.all_home_crosses_key) and bool(r.all_away_crosses_key)            # both sides beat the 3
    assert (r.ny_best_away_spread, r.ny_best_away_book) == (3.0, "draftkings")     # NY-only: no 3.5 (offshore); best price at +3
    assert r.ny_best_away_price == -110 and r.ref_book == "pinnacle" and r.ref_home_spread == -3.0
    assert not bool(r.ny_away_crosses_key)


def test_movement_first_last_key_numbers_and_close_quality():
    early = _q(SNAP - timedelta(days=2), [spread_book(b, -2.5, -110, 2.5, -110) for b in ("draftkings", "fanduel")])
    late = _q(SNAP, [spread_book(b, -3.5, -110, 3.5, -110) for b in ("draftkings", "fanduel", "betmgm", "betrivers", "espnbet")])
    ls = build_spread_line_shopping(pd.concat([early, late], ignore_index=True))
    m = build_spread_movement(ls, {"2026_07_SEA_DEN": 7.0}).iloc[0]
    assert (m.first_observed_home_spread, m.last_observed_pregame_home_spread, m.move_pts) == (-2.5, -3.5, -1.0)
    assert m.key_numbers_crossed == "3" and bool(m.close_quality) and m.actual_home_margin == 7.0


def test_in_play_spreads_are_excluded():
    assert build_spread_line_shopping(_q(SNAP + timedelta(hours=1), [spread_book("draftkings", -3.0, -110, 3.0, -110)])).empty
