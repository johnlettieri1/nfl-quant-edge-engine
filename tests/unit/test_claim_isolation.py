"""Correction 1: a claim is resolved ONLY by its own capture (raw envelope claim_id), never by an unrelated
capture of the same kind. Real local git repositories for the job simulations; no network, no credits."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from src.nfl.data import odds_snapshot as O
from src.nfl.market import phase4_config as C
from src.nfl.ops import claims as CL
from src.nfl.ops import monitor as M
from tests.unit._phase4_fixtures import SCHED
from tests.unit.test_paid_pull_races import (commit_capture, git, job, origin, pregame_claim, race,  # noqa: F401
                                             sched_claim, u)

K = u(2026, 10, 25, 17, 0)                                   # 1:00 PM EDT slot
GA = {"event_id": "gameA", "kickoff": K.isoformat()}
GB = {"event_id": "gameB", "kickoff": K.isoformat()}
NOW = K - timedelta(minutes=22)                               # inside every game's T-30..T-10 window


def _env(odds, dt, kind, claim_id):
    (odds / "raw").mkdir(parents=True, exist_ok=True)
    (odds / "raw" / f"totals_{dt:%Y%m%dT%H%M%SZ}_{kind}.json").write_text(
        json.dumps({"snapshot_kind": kind, "claim_id": claim_id}))


def _claim(odds, dt, kind, run="7-1", events=()):
    (odds / "claims").mkdir(parents=True, exist_ok=True)
    name = CL.claim_filename(dt, kind, run)
    (odds / "claims" / name).write_text(json.dumps({"claim_id": name[:-5], "claim_ts": dt.isoformat(), "kind": kind,
                                                   "run_id": run, "event_ids": list(events), "scheduled_target": None}))
    return name


# ---- unit: the association itself ----------------------------------------------------------------

def test_unrelated_capture_of_the_same_kind_never_resolves_a_claim(tmp_path):
    odds = tmp_path / "odds"
    a = _claim(odds, NOW, "pregame_window", "1-1", ["gameA"])
    b = _claim(odds, NOW + timedelta(seconds=5), "pregame_window", "2-1", ["gameB"])
    _env(odds, NOW + timedelta(seconds=30), "pregame_window", a[:-5])          # job A's capture
    cap = CL.capture_claim_ids(odds, NOW - timedelta(hours=1))
    claims = {c["_file"]: c for c in CL.load_claims(odds)}
    assert CL.resolved(claims[a], cap) and not CL.resolved(claims[b], cap)
    assert [c["_file"] for c in CL.active_claims(list(claims.values()), cap, NOW + timedelta(minutes=2))] == [b]


def test_captures_without_claim_id_or_with_another_kind_resolve_nothing(tmp_path):
    odds = tmp_path / "odds"
    a = _claim(odds, NOW, "pregame_window", "1-1", ["gameA"])
    _env(odds, NOW + timedelta(seconds=10), "pregame_window", None)             # pre-claim / manual capture
    _env(odds, NOW + timedelta(seconds=20), "sun_scheduled", a[:-5])            # id copied onto the wrong kind
    cap = CL.capture_claim_ids(odds, NOW - timedelta(hours=1))
    assert not CL.resolved(CL.load_claims(odds)[0], cap)


def test_take_snapshot_writes_its_claim_id_and_rejects_a_mismatched_one_before_paying(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(O, "fetch_balance", lambda: {"remaining": 400, "used": 1})
    monkeypatch.setattr(O, "fetch_totals_raw", lambda books: calls.append(1) or {
        "endpoint": "/v4", "request_params": {}, "http_status": 200, "retrieval_started_at": NOW.isoformat(),
        "fetched_at": NOW.isoformat(), "body": b"[]", "x_requests_remaining": "399", "x_requests_used": "2",
        "x_requests_last": "1"})
    good = CL.claim_filename(NOW, "manual", "5-1")[:-5]
    with pytest.raises(ValueError, match="does not match"):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=NOW,
                        claim_id=CL.claim_filename(NOW, "pregame_window", "5-1")[:-5])
    with pytest.raises(ValueError, match="does not match"):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=NOW, claim_id="not-a-claim")
    assert calls == []
    s = O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=NOW, claim_id=good)
    env = json.loads(open(s["raw_path"]).read())
    assert env["claim_id"] == good and s["claim_id"] == good and calls == [1]


# ---- job simulations ------------------------------------------------------------------------------

def test_independent_claims_the_scenario_from_review(tmp_path, origin):  # noqa: F811
    ja, jb, jc = (job(tmp_path, origin, n) for n in ("A", "B", "C"))
    da = pregame_claim(ja, NOW, [GA], run="31")                    # Job A claims Game A
    db = pregame_claim(jb, NOW + timedelta(seconds=10), [GB], run="32")   # Job B claims Game B
    assert da.pay and db.pay
    commit_capture(ja, NOW + timedelta(seconds=40), "pregame_window", ["gameA"], claim=da)   # A captures Game A
    # Job B is still processing. Job C checks Game B: A's capture must NOT release B's claim.
    dc = pregame_claim(jc, NOW + timedelta(minutes=2), [GB], run="33")
    assert (dc.pay, dc.state) == (False, "claimed_elsewhere")
    # and Game A is now covered by A's real quotes
    dc2 = pregame_claim(jc, NOW + timedelta(minutes=2), [GA], run="34")
    assert (dc2.pay, dc2.state) == (False, "not_needed")


def test_own_capture_with_partial_game_coverage_resolves_the_claim(tmp_path, origin):  # noqa: F811
    ja, jc = job(tmp_path, origin, "A"), job(tmp_path, origin, "C")
    da = pregame_claim(ja, NOW, [GA, GB], run="41")
    assert da.pay and [t["event_id"] for t in da.targets] == ["gameA", "gameB"]
    commit_capture(ja, NOW + timedelta(seconds=30), "pregame_window", ["gameA"], claim=da)   # gameB not quoted
    # the claim is resolved by its own capture; per-game coverage now decides: gameB may be retried
    dc = pregame_claim(jc, NOW + timedelta(minutes=3), [GA, GB], run="42")
    assert dc.pay and [t["event_id"] for t in dc.targets] == ["gameB"]


def test_overlapping_jobs_for_different_games_both_pay_and_stay_isolated(tmp_path, origin):  # noqa: F811
    ja, jb, jc = (job(tmp_path, origin, n) for n in ("A", "B", "C"))
    res = race([lambda bp: pregame_claim(ja, NOW, [GA], before_push=bp, run="51"),
                lambda bp: pregame_claim(jb, NOW, [GB], before_push=bp, run="52")])
    assert [d.pay for d in res] == [True, True]                     # different games: both legitimate
    commit_capture(jb, NOW + timedelta(seconds=30), "pregame_window", ["gameB"], claim=res[1])
    dc = pregame_claim(jc, NOW + timedelta(minutes=1), [GA, GB], run="53")
    assert (dc.pay, dc.state) == (False, "claimed_elsewhere")      # gameA still held by A; gameB covered


def test_expired_claim_without_its_capture_stops_blocking(tmp_path, origin):  # noqa: F811
    jb, jc = job(tmp_path, origin, "B"), job(tmp_path, origin, "C")
    db = pregame_claim(jb, K - timedelta(minutes=29, seconds=50), [GB], run="61")
    assert db.pay                                                  # B claimed, then crashed or never captured
    early = K - timedelta(minutes=29, seconds=50) + timedelta(minutes=C.CLAIM_TTL_MIN - 1)
    assert pregame_claim(jc, early, [GB], run="62").state == "claimed_elsewhere"
    later = K - timedelta(minutes=29, seconds=50) + timedelta(minutes=C.CLAIM_TTL_MIN, seconds=5)   # T-9:45
    dc = pregame_claim(jc, later, [GB], run="63")
    assert dc.pay and [t["event_id"] for t in dc.targets] == ["gameB"]   # late attempt inside T-10..T-5


def test_unrelated_scheduled_capture_does_not_resolve_a_scheduled_claim(tmp_path, origin):  # noqa: F811
    thu = u(2026, 10, 22, 13, 10)
    ja, jb = job(tmp_path, origin, "A"), job(tmp_path, origin, "B")
    da = sched_claim(ja, thu + timedelta(seconds=30), "thu_scheduled", thu, run="71")
    assert da.pay
    commit_capture(jb, thu + timedelta(seconds=40), "manual")      # some other capture lands meanwhile
    odds = jb / "data" / "odds"
    git(jb, "pull", "-q", "--rebase", "origin", "records")
    claims = CL.load_claims(odds)
    assert len(claims) == 1 and not CL.resolved(claims[0], CL.capture_claim_ids(odds, thu))


# ---- monitor uses the same association ------------------------------------------------------------

def test_monitor_interpretation_of_independent_partial_and_expired_claims(tmp_path):
    odds = tmp_path / "odds"
    t = u(2026, 10, 25, 16, 38)
    a = _claim(odds, t, "pregame_window", "81-1", ["gameA", "gameB"])          # resolved, partial coverage
    b = _claim(odds, t + timedelta(seconds=5), "pregame_window", "82-1", ["gameC"])   # never captured
    c = _claim(odds, t + timedelta(seconds=9), "pregame_window", "83-1", ["gameD"])   # own capture after TTL
    _env(odds, t + timedelta(seconds=30), "pregame_window", a[:-5])
    _env(odds, t + timedelta(minutes=C.CLAIM_TTL_MIN + 5), "pregame_window", c[:-5])
    later = t + timedelta(hours=2)
    keys = [f["key"] for f in M.unresolved_claims(later, odds, u(2026, 10, 22, 0, 0))]
    assert keys == [f"claim-unresolved-{b}"]                     # A's capture does not hide B; C's own late capture counts
    assert M.unresolved_claims(t + timedelta(minutes=10), odds, u(2026, 10, 22, 0, 0)) == []   # too early to judge
    assert [f["title"] for f in M.unresolved_claims(later + timedelta(hours=6), odds, u(2026, 10, 22, 0, 0))] == \
        [f"[collection-alert] Paid pull claimed but no capture in records: {b}"]           # idempotent title
