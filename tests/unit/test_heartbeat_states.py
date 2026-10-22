"""Safeguard 2: success heartbeats only for verified work. Unit tests of the decision plus an execution of
the real workflow heartbeat scripts (extracted from the YAML) with a stub `curl` that records each ping."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from src.nfl.ops.heartbeat import pregame_state, settle_state, snapshot_state

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows"
SLUG = "nfl-thu-scheduled"


# ---- scheduled snapshot: the five required states -------------------------------------------------

def test_1_newly_captured_and_verified_on_remote_is_success():
    assert snapshot_state(heartbeat=SLUG, gate_kind="thu_scheduled", claim_state="pay", claim_outcome="success",
                          odds_outcome="success", raw_file="totals_x_thu_scheduled.json", verified="true") == \
        ("captured_committed", "success")


@pytest.mark.parametrize("kw", [
    {"gate_kind": "none"},                                                    # gate: fresh records already have it
    {"gate_kind": "thu_scheduled", "claim_state": "already_captured", "claim_outcome": "success"},
    {"gate_kind": "thu_scheduled", "claim_state": "pay", "claim_outcome": "success", "odds_outcome": "success",
     "skip_reason": "already_captured"}])                                    # final pre-pay re-check
def test_2_already_captured_in_records_is_success(kw):
    assert snapshot_state(heartbeat=SLUG, **kw) == ("already_captured", "success")


@pytest.mark.parametrize("kw", [
    {"heartbeat": "", "gate_kind": "none"},                                  # no live window at all
    {"heartbeat": "", "gate_kind": "manual"},                                # manual snapshot: not a scheduled window
    {"heartbeat": SLUG, "gate_kind": "thu_scheduled", "claim_state": "obsolete", "claim_outcome": "success"},
    {"heartbeat": SLUG, "gate_kind": "thu_scheduled", "claim_state": "claimed_elsewhere", "claim_outcome": "success"},
    {"heartbeat": SLUG, "gate_kind": "thu_scheduled", "claim_state": "pay", "claim_outcome": "success",
     "odds_outcome": "success", "skip_reason": "obsolete"}])
def test_3_legitimate_skips_send_no_ping(kw):
    assert snapshot_state(**kw)[1] == "none"


@pytest.mark.parametrize("kw", [
    {"claim_state": "claim_failed", "claim_outcome": "failure"},             # could not record a claim
    {"claim_state": "", "claim_outcome": "failure"},                         # claim step crashed
    {"claim_state": "", "claim_outcome": "cancelled"},                       # aborted
    {"claim_state": "pay", "claim_outcome": "success", "odds_outcome": "failure"},          # budget refusal / API error
    {"claim_state": "pay", "claim_outcome": "success", "odds_outcome": "cancelled"},
    {"claim_state": "pay", "claim_outcome": "success", "odds_outcome": "success"}])         # success but no raw file
def test_4_failed_or_aborted_is_fail(kw):
    assert snapshot_state(heartbeat=SLUG, gate_kind="thu_scheduled", **kw) == ("failed", "fail")


@pytest.mark.parametrize("verified", ["false", ""])
def test_5_paid_capture_not_on_remote_records_is_fail_never_success(verified):
    assert snapshot_state(heartbeat=SLUG, gate_kind="thu_scheduled", claim_state="pay", claim_outcome="success",
                          odds_outcome="success", raw_file="totals_x_thu_scheduled.json", verified=verified,
                          job_status="success") == ("captured_not_committed", "fail")


def test_job_success_alone_never_produces_a_success_ping():
    assert snapshot_state(heartbeat=SLUG, gate_kind="thu_scheduled", job_status="success")[1] == "fail"


# ---- pregame and settlement ----------------------------------------------------------------------

def test_pregame_states():
    ok = dict(claim_state="pay", claim_outcome="success", odds_outcome="success", raw_file="r.json")
    assert pregame_state(**ok, verified="true") == ("captured_committed", "success")
    assert pregame_state(**ok, verified="false") == ("captured_not_committed", "fail")
    assert pregame_state(claim_state="not_needed", claim_outcome="success")[1] == "none"
    assert pregame_state(claim_state="claimed_elsewhere", claim_outcome="success")[1] == "none"
    assert pregame_state(claim_state="pay", claim_outcome="success", skip_reason="not_needed")[1] == "none"
    assert pregame_state(claim_state="claim_failed", claim_outcome="failure")[1] == "fail"
    assert pregame_state(claim_state="pay", claim_outcome="success", odds_outcome="failure")[1] == "fail"


@pytest.mark.parametrize("settle,commit,verify,expected", [
    ("success", "pushed", "success", ("settled_committed", "success")),
    ("success", "nothing", "success", ("verified_idempotent", "success")),
    ("success", "failed", "skipped", ("not_committed", "fail")),        # push failed
    ("success", "", "skipped", ("not_committed", "fail")),              # guard refused, commit never ran
    ("success", "pushed", "failure", ("unverified", "fail")),           # remote ledgers missing expected keys
    ("success", "nothing", "failure", ("unverified", "fail")),
    ("failure", "", "", ("failed", "fail")),                            # nflverse fetch or settlement crashed
    ("cancelled", "", "", ("failed", "fail"))])
def test_settlement_states(settle, commit, verify, expected):
    assert settle_state(settle_outcome=settle, commit_result=commit, verify_outcome=verify) == expected


# ---- the real workflow scripts, executed with a stub curl -----------------------------------------

def _step(name, step_name):
    d = yaml.safe_load((WF / name).read_text())
    steps = next(iter(d["jobs"].values()))["steps"] if name != "pregame.yml" else d["jobs"]["pull"]["steps"]
    return next(s for s in steps if s.get("name") == step_name)


def _run_step(tmp_path, step, env):
    stub = tmp_path / "bin"; stub.mkdir(exist_ok=True)
    log = tmp_path / "curl.log"
    (stub / "curl").write_text(f'#!/bin/bash\necho "$@" >> {log}\n')
    (stub / "curl").chmod((stub / "curl").stat().st_mode | stat.S_IEXEC)
    script = step["run"].replace("${{ github.run_id }}", "123")
    full = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "PYTHONPATH": str(ROOT), "HC_PING_KEY": "PINGKEY", **env}
    r = subprocess.run(["bash", "-e", "-c", script], cwd=ROOT, env=full, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return log.read_text().splitlines() if log.exists() else []


WEEKLY_BASE = {"HB_SLUG": SLUG, "GATE_KIND": "thu_scheduled", "CLAIM_STATE": "pay", "CLAIM_OUTCOME": "success",
               "ODDS_OUTCOME": "success", "SKIP_REASON": "", "RAW_FILE": "totals_x.json", "VERIFIED": "true",
               "JOB_STATUS": "success"}


@pytest.mark.parametrize("override,expect", [
    ({}, "success"),                                                         # 1
    ({"GATE_KIND": "none", "CLAIM_STATE": "", "RAW_FILE": "", "VERIFIED": ""}, "success"),   # 2
    ({"HB_SLUG": "", "GATE_KIND": "none"}, None),                           # 3
    ({"CLAIM_STATE": "claimed_elsewhere", "RAW_FILE": "", "VERIFIED": ""}, None),            # 3
    ({"ODDS_OUTCOME": "failure", "RAW_FILE": "", "VERIFIED": "", "JOB_STATUS": "failure"}, "fail"),   # 4
    ({"VERIFIED": "false", "JOB_STATUS": "failure"}, "fail")])               # 5
def test_weekly_heartbeat_script(tmp_path, override, expect):
    calls = _run_step(tmp_path, _step("weekly.yml", "Heartbeat"), {**WEEKLY_BASE, **override})
    if expect is None:
        assert calls == []
    else:
        assert len(calls) == 1
        url = calls[0].split()[-1]
        assert url == f"https://hc-ping.com/PINGKEY/{SLUG}" + ("" if expect == "success" else "/fail")


@pytest.mark.parametrize("env,expect", [
    ({"SETTLE_OUTCOME": "success", "COMMIT_RESULT": "nothing", "VERIFY_OUTCOME": "success"}, ""),
    ({"SETTLE_OUTCOME": "success", "COMMIT_RESULT": "failed", "VERIFY_OUTCOME": "skipped"}, "/fail"),
    ({"SETTLE_OUTCOME": "success", "COMMIT_RESULT": "pushed", "VERIFY_OUTCOME": "failure"}, "/fail")])
def test_settle_heartbeat_script(tmp_path, env, expect):
    calls = _run_step(tmp_path, _step("settle.yml", "Heartbeat"), env)
    assert [c.split()[-1] for c in calls] == [f"https://hc-ping.com/PINGKEY/nfl-settle{expect}"]


def test_pregame_heartbeat_script_skip_sends_nothing(tmp_path):
    assert _run_step(tmp_path, _step("pregame.yml", "Heartbeat"),
                     {"CLAIM_STATE": "claimed_elsewhere", "CLAIM_OUTCOME": "success"}) == []


def test_no_ping_without_key(tmp_path):
    assert _run_step(tmp_path, _step("weekly.yml", "Heartbeat"), {**WEEKLY_BASE, "HC_PING_KEY": ""}) == []


def test_workflows_have_no_unconditional_success_ping():
    for name in ("weekly.yml", "pregame.yml", "settle.yml"):
        t = (WF / name).read_text()
        assert "if: success()" not in t, name
        assert "python -m src.nfl.ops.heartbeat" in t, name
    w = (WF / "weekly.yml").read_text()
    assert 'cat-file -e "FETCH_HEAD:data/odds/raw/$RAW_FILE"' in w
    assert "steps.claim.outputs.pay == 'true'" in w
    s = (WF / "settle.yml").read_text()
    assert "python run.py settle-verify" in s and "git -C records archive FETCH_HEAD data/predictions" in s
