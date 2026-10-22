"""Safeguard 3: duplicate paid-pull race simulation (real local git repositories, no network, no credits).

Each "job" is a separate clone of a bare `origin` holding the records branch, exactly like a workflow
runner. A job "pays" only when its decision says pay; the tests count those decisions. Barriers force the
worst interleaving: every job passes its records check before any job pushes.
"""
from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import pytest

from src.nfl.market import phase4_config as C
from src.nfl.market import timing as T
from src.nfl.ops import claims as CL

UTC = timezone.utc
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=records"]


def u(*a):
    return datetime(*a, tzinfo=UTC)


def git(cwd, *args, check=True):
    return subprocess.run([*GIT, *args], cwd=cwd, check=check, capture_output=True, text=True)


@pytest.fixture
def origin(tmp_path):
    seed = tmp_path / "seed"
    (seed / "data" / "odds" / "raw").mkdir(parents=True)
    (seed / "data" / "odds" / "raw" / ".gitkeep").write_text("")
    git(tmp_path, "init", "-q", str(seed))
    git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


def job(tmp_path, origin, name):
    d = tmp_path / name
    git(tmp_path, "clone", "-q", "--branch", "records", str(origin), str(d))
    return d


def commit_capture(repo, dt, kind, event_ids=(), claim=None):
    """What a paying job does after the API call: write the raw envelope (carrying its claim id, as
    take_snapshot does) and paired quotes, commit, push (rebasing on other records commits, like the workflow)."""
    raw = repo / "data" / "odds" / "raw" / f"totals_{dt:%Y%m%dT%H%M%SZ}_{kind}.json"
    raw.write_text(json.dumps({"schema_version": "odds_raw_v2", "snapshot_kind": kind,
                               "claim_id": CL.claim_id_of(claim.claim_file) if claim and claim.claim_file else None}))
    if event_ids:
        q = repo / "data" / "odds" / "quotes"; q.mkdir(parents=True, exist_ok=True)
        rows = "".join(f"{dt.isoformat()},{e},draftkings,44.5,-110,-110\n" for e in event_ids)
        (q / f"quotes_{dt:%Y%m%dT%H%M%SZ}_{kind}.csv").write_text("snapshot_ts_utc,event_id,book,total_line,over_price,under_price\n" + rows)
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", f"capture {kind}")
    for _ in range(3):
        if git(repo, "push", "-q", "origin", "HEAD:refs/heads/records", check=False).returncode == 0:
            return
        git(repo, "pull", "-q", "--rebase", "origin", "records")
    raise AssertionError("capture push failed")


def sched_claim(repo, now, kind, target, trigger="external_dispatch", before_push=None, run="1"):
    return CL.claim_with_cas(repo, lambda od: CL.decide_scheduled(now, kind, target, od, trigger),
                             lambda d: {"scheduled_target": target.isoformat(), "event_ids": [], "trigger": trigger},
                             lambda: now, run, kind, before_push=before_push)


def pregame_claim(repo, now, targets, before_push=None, run="1"):
    return CL.claim_with_cas(repo, lambda od: CL.decide_pregame(now, targets, od),
                             lambda d: {"scheduled_target": None, "event_ids": [t["event_id"] for t in d.targets],
                                        "trigger": "external_dispatch"},
                             lambda: now, run, "pregame_window", before_push=before_push)


def race(fns):
    """Run claim functions in threads; a barrier makes every job finish its records check before any push."""
    barrier = threading.Barrier(len(fns), timeout=30)
    out = [None] * len(fns)

    def run(i):
        first = [True]

        def wait_first_push_only():      # a retry after a rejected push must not wait again
            if first[0]:
                first[0] = False
                barrier.wait()
        out[i] = fns[i](wait_first_push_only)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(len(fns))]
    [t.start() for t in ts]; [t.join(60) for t in ts]
    return out


THU = u(2026, 10, 22, 13, 10)                         # thu_scheduled 9:10 AM EDT
LON = u(2026, 10, 25, 13, 30)                         # 9:30 AM EDT international kickoff
SUN = u(2026, 10, 25, 13, 10)                         # sun_scheduled 9:10 AM EDT


# ---- 0. the race exists without claims ------------------------------------------------------------

def test_without_claims_two_jobs_that_check_before_either_commits_would_both_pay():
    # this is why claims are needed: the pre-pay checks alone see the same empty records state
    assert T.scheduled_gate(THU + timedelta(seconds=30), []).kind == "thu_scheduled"
    assert T.scheduled_gate(THU + timedelta(seconds=45), []).kind == "thu_scheduled"
    # weekly (sun_scheduled late by 9 min) and pregame (London, after the 8-min deferral) both decide to pay
    assert T.scheduled_gate(SUN + timedelta(minutes=9), []).kind == "sun_scheduled"
    assert T.plan_pregame(LON - timedelta(minutes=11), [("lon", LON)], []).pull


# ---- 1/2. two external triggers together; external + GitHub cron overlap --------------------------

def test_two_external_triggers_arriving_together_pay_once(tmp_path, origin):
    a, b = job(tmp_path, origin, "a"), job(tmp_path, origin, "b")
    now = THU + timedelta(seconds=40)
    res = race([lambda bp, r=a: sched_claim(r, now, "thu_scheduled", THU, before_push=bp, run="11"),
                lambda bp, r=b: sched_claim(r, now, "thu_scheduled", THU, before_push=bp, run="12")])
    assert sorted(d.state for d in res) == ["claimed_elsewhere", "pay"]
    assert sum(d.pay for d in res) == 1


def test_external_and_cron_overlap_pay_once_before_and_after_the_capture_lands(tmp_path, origin):
    ext, cron1, cron2 = (job(tmp_path, origin, n) for n in ("ext", "cron1", "cron2"))
    d1 = sched_claim(ext, THU + timedelta(seconds=30), "thu_scheduled", THU)
    assert d1.pay
    # cron twin starts while the external job is still running (claim on records, capture not yet committed)
    d2 = sched_claim(cron1, THU + timedelta(minutes=2), "thu_scheduled", THU, trigger="github_cron")
    assert (d2.pay, d2.state) == (False, "claimed_elsewhere")
    commit_capture(ext, THU + timedelta(seconds=35), "thu_scheduled")
    # the other twin, hours later: the capture itself now blocks it, long after the claim expired
    d3 = sched_claim(cron2, THU + timedelta(hours=3), "thu_scheduled", THU, trigger="github_cron")
    assert (d3.pay, d3.state) == (False, "already_captured")


def test_many_simultaneous_jobs_pay_exactly_once_for_one_target(tmp_path, origin):
    repos = [job(tmp_path, origin, f"j{i}") for i in range(5)]
    now = THU + timedelta(seconds=50)
    res = race([lambda bp, r=r, i=i: sched_claim(r, now, "thu_scheduled", THU, before_push=bp, run=str(i)) for i, r in enumerate(repos)])
    assert sum(d.pay for d in res) == 1 and all(d.state in ("pay", "claimed_elsewhere") for d in res)


# ---- 3. weekly and pregame overlapping -----------------------------------------------------------

def test_scheduled_claim_inside_a_game_window_stops_the_pregame_pull(tmp_path, origin):
    wk, pg = job(tmp_path, origin, "weekly"), job(tmp_path, origin, "pregame")
    assert sched_claim(wk, SUN + timedelta(minutes=2), "sun_scheduled", SUN).pay      # weekly claims 9:12
    tgt = [{"event_id": "lon", "kickoff": LON.isoformat()}]
    d = pregame_claim(pg, LON - timedelta(minutes=11), tgt)                           # pregame 9:19, after deferral
    assert (d.pay, d.state) == (False, "claimed_elsewhere")
    commit_capture(wk, SUN + timedelta(minutes=2, seconds=5), "sun_scheduled", ["lon"])
    d = pregame_claim(pg, LON - timedelta(minutes=8), tgt)                            # capture quoted the game
    assert (d.pay, d.state) == (False, "not_needed")


def test_weekly_and_pregame_racing_from_the_same_state(tmp_path, origin):
    wk, pg = job(tmp_path, origin, "weekly"), job(tmp_path, origin, "pregame")
    tgt = [{"event_id": "lon", "kickoff": LON.isoformat()}]
    now = SUN + timedelta(minutes=9)                                                  # weekly 9 min late
    res = race([lambda bp: sched_claim(wk, now, "sun_scheduled", SUN, trigger="github_cron", before_push=bp),
                lambda bp: pregame_claim(pg, now, tgt, before_push=bp)])
    weekly, pregame = res
    # The scheduled snapshot is a different snapshot kind and is always taken. If the weekly claim wins, the
    # pregame job re-decides and skips; if the pregame claim wins, the weekly still pays for its own kind.
    assert weekly.pay
    assert pregame.state in ("pay", "claimed_elsewhere")
    paid = sum(d.pay for d in res)
    assert paid in (1, 2)          # 2 = documented residual: distinct kinds serving one game window


def test_pregame_claimed_first_then_late_weekly_is_a_documented_second_credit(tmp_path, origin):
    pg, wk = job(tmp_path, origin, "pregame"), job(tmp_path, origin, "weekly")
    tgt = [{"event_id": "lon", "kickoff": LON.isoformat()}]
    assert pregame_claim(pg, LON - timedelta(minutes=12), tgt).pay                     # 9:18, weekly never landed
    assert sched_claim(wk, LON - timedelta(minutes=10), "sun_scheduled", SUN, trigger="github_cron").pay
    # two paid requests serve this game's window: residual risk, documented (never two of the SAME kind)


# ---- 4. two pregame jobs pass their checks before either commits ----------------------------------

def test_two_pregame_jobs_racing_pay_once_per_game(tmp_path, origin):
    a, b = job(tmp_path, origin, "a"), job(tmp_path, origin, "b")
    k = u(2026, 10, 25, 17, 0)
    tgt = [{"event_id": "g1", "kickoff": k.isoformat()}, {"event_id": "g2", "kickoff": k.isoformat()}]
    now = k - timedelta(minutes=20)
    res = race([lambda bp: pregame_claim(a, now, tgt, before_push=bp, run="21"),
                lambda bp: pregame_claim(b, now, tgt, before_push=bp, run="22")])
    assert sum(d.pay for d in res) == 1
    assert [d.state for d in res if not d.pay] == ["claimed_elsewhere"]


def test_partial_overlap_buys_only_the_unclaimed_game(tmp_path, origin):
    a, b = job(tmp_path, origin, "a"), job(tmp_path, origin, "b")
    k1, k2 = u(2026, 10, 25, 20, 5), u(2026, 10, 25, 20, 25)        # 4:05 and 4:25 PM EDT
    now = k1 - timedelta(minutes=12)                                 # 3:53: 4:05 in window, 4:25 not yet
    d1 = pregame_claim(a, now, [{"event_id": "early", "kickoff": k1.isoformat()}])
    assert d1.pay
    later = k2 - timedelta(minutes=28)                               # 3:57: both in window
    d2 = pregame_claim(b, later, [{"event_id": "early", "kickoff": k1.isoformat()},
                                  {"event_id": "late", "kickoff": k2.isoformat()}])
    assert d2.pay and [t["event_id"] for t in d2.targets] == ["late"]


# ---- 5. records push failing or delayed -----------------------------------------------------------

def test_claim_push_failure_means_no_payment(tmp_path, origin):
    a = job(tmp_path, origin, "a")
    git(a, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    d = sched_claim(a, THU + timedelta(seconds=30), "thu_scheduled", THU)
    assert (d.pay, d.state) == (False, "claim_failed")


def test_capture_push_failure_blocks_others_until_ttl_then_a_second_credit_is_possible(tmp_path, origin):
    a, b, c = (job(tmp_path, origin, n) for n in ("a", "b", "c"))
    assert sched_claim(a, THU + timedelta(seconds=30), "thu_scheduled", THU).pay
    # job a paid but its records push failed: the claim is on records, the capture is not
    d = sched_claim(b, THU + timedelta(minutes=C.CLAIM_TTL_MIN - 1), "thu_scheduled", THU, trigger="github_cron")
    assert (d.pay, d.state) == (False, "claimed_elsewhere")
    d = sched_claim(c, THU + timedelta(minutes=C.CLAIM_TTL_MIN + 1), "thu_scheduled", THU, trigger="github_cron")
    assert d.pay        # documented residual: after the TTL a second paid pull can happen (the first was lost)


def test_delayed_capture_push_longer_than_ttl_can_duplicate_then_is_deduped_by_the_capture(tmp_path, origin):
    a, b, c = (job(tmp_path, origin, n) for n in ("a", "b", "c"))
    assert sched_claim(a, THU + timedelta(seconds=30), "thu_scheduled", THU).pay
    assert sched_claim(b, THU + timedelta(minutes=C.CLAIM_TTL_MIN + 2), "thu_scheduled", THU, trigger="github_cron").pay
    commit_capture(a, THU + timedelta(seconds=35), "thu_scheduled")                  # a's slow push lands
    d = sched_claim(c, THU + timedelta(hours=2), "thu_scheduled", THU, trigger="github_cron")
    assert d.state == "already_captured"


def test_stale_claim_from_a_crashed_job_expires_no_deadlock(tmp_path, origin):
    a, b = job(tmp_path, origin, "a"), job(tmp_path, origin, "b")
    assert sched_claim(a, THU + timedelta(seconds=30), "thu_scheduled", THU).pay     # crashes before paying
    d = sched_claim(b, THU + timedelta(minutes=C.CLAIM_TTL_MIN, seconds=31), "thu_scheduled", THU, trigger="github_cron")
    assert d.pay                                                                      # legitimate capture proceeds


def test_obsolete_window_never_claims(tmp_path, origin):
    a = job(tmp_path, origin, "a")
    d = sched_claim(a, THU + timedelta(hours=19), "thu_scheduled", THU, trigger="github_cron")
    assert (d.pay, d.state) == (False, "obsolete")
    assert not (a / "data" / "odds" / "claims").exists()


def test_claims_are_new_files_only_and_pass_the_records_guard(tmp_path, origin):
    from src.nfl.ops.records_guard import evaluate
    a = job(tmp_path, origin, "a")
    before = git(a, "rev-parse", "HEAD").stdout.strip()
    d = sched_claim(a, THU + timedelta(seconds=30), "thu_scheduled", THU)
    diff = git(a, "diff", "--name-status", before, "HEAD").stdout.split("\n")
    changes = [tuple(x.split("\t")) for x in diff if x]
    assert changes == [("A", f"data/odds/claims/{d.claim_file}")]
    body = json.loads((a / "data" / "odds" / "claims" / d.claim_file).read_text())
    assert body["kind"] == "thu_scheduled" and body["scheduled_target"] == THU.isoformat()
    assert evaluate(changes, lambda p: b"", lambda p: (a / p).read_bytes()) == []


def test_claim_cli_end_to_end_outputs_and_second_job_is_blocked(tmp_path, origin):
    """`run.py claim` exactly as the workflows call it (real clock, manual trigger so any window is allowed)."""
    import sys
    from datetime import datetime as dt
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    tgt = T._latest_target("thu_scheduled", dt.now(UTC)).isoformat()
    outs = []
    for name in ("j1", "j2"):
        repo = job(tmp_path, origin, name)
        odds = tmp_path / f"{name}_work" / "data" / "odds"; odds.mkdir(parents=True)
        gh = tmp_path / f"{name}.out"
        r = subprocess.run([sys.executable, "run.py", "claim", "--repo", str(repo), "--odds-dir", str(odds),
                            "--mode", "scheduled", "--kind", "thu_scheduled", "--target", tgt, "--trigger", "manual",
                            "--run-id", f"9{name[-1]}-1", "--github-output", str(gh)], cwd=root, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        outs.append(dict(ln.split("=", 1) for ln in gh.read_text().splitlines()))
        if outs[-1]["pay"] == "true":
            assert (odds / "claims" / outs[-1]["claim"]).exists()          # working copy refreshed for the re-check
    assert [o["pay"] for o in outs] == ["true", "false"] and outs[1]["state"] == "claimed_elsewhere"
