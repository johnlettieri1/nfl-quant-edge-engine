"""Heartbeat decisions (Safeguard 2): a success ping only for work that is verified on the records branch.

A workflow that exits successfully is NOT enough. The workflows pass their step outcomes here and ping
healthchecks.io with the returned action:

Scheduled snapshot (weekly.yml), slug = the scheduled kind whose window the run served:
  1. captured_committed      paid capture, records push done, raw file verified on remote `records`  -> success
  2. already_captured        the records already hold this (kind, target) capture                     -> success
  3. skipped                 no live window, obsolete, or another run holds the claim                 -> no ping
  4. failed                  any failure or abort before a verified capture                           -> fail
  5. captured_not_committed  paid capture exists only on the runner (push failed or unverified)       -> fail

Pregame pull (pregame.yml, slug nfl-pregame): success only for case 1; skips send nothing.
Settlement (settle.yml, slug nfl-settle): success only when settlement completed, its records push either
succeeded or had nothing new to commit, AND a read-back of the remote ledgers shows every expected settlement
key for verified finals (settle-verify). Otherwise fail.

Standard library only. CLI: `python -m src.nfl.ops.heartbeat snapshot|pregame|settle` reads the step
outcomes from environment variables and prints "<state> <action>" (action: success, fail or none).
"""
from __future__ import annotations

import os
import sys

SUCCESS, FAIL, NONE = "success", "fail", "none"


def snapshot_state(*, heartbeat: str, gate_kind: str, claim_state: str = "", claim_outcome: str = "",
                   odds_outcome: str = "", skip_reason: str = "", raw_file: str = "", verified: str = "",
                   job_status: str = "") -> tuple[str, str]:
    if not heartbeat:                                   # no scheduled window served (or a manual snapshot)
        return "skipped", NONE
    if gate_kind == "none":                             # window live and the fresh records clone already has it
        return "already_captured", SUCCESS
    if claim_state == "already_captured":               # re-checked on a fresh fetch right before paying
        return "already_captured", SUCCESS
    if claim_state in ("obsolete", "claimed_elsewhere"):
        return "skipped", NONE                          # the claiming run reports its own result
    if claim_state != "pay" or claim_outcome not in ("success", ""):
        return "failed", FAIL
    if skip_reason == "already_captured":
        return "already_captured", SUCCESS
    if skip_reason in ("obsolete", "not_needed"):
        return "skipped", NONE
    if raw_file:
        return ("captured_committed", SUCCESS) if verified == "true" else ("captured_not_committed", FAIL)
    return "failed", FAIL


def pregame_state(*, claim_state: str = "", claim_outcome: str = "", odds_outcome: str = "", skip_reason: str = "",
                  raw_file: str = "", verified: str = "", job_status: str = "") -> tuple[str, str]:
    if claim_state in ("not_needed", "claimed_elsewhere"):
        return "skipped", NONE
    if claim_state != "pay" or claim_outcome not in ("success", ""):
        return "failed", FAIL
    if skip_reason:
        return "skipped", NONE                          # final re-check found nothing to buy (no credit)
    if raw_file:
        return ("captured_committed", SUCCESS) if verified == "true" else ("captured_not_committed", FAIL)
    return "failed", FAIL


def settle_state(*, settle_outcome: str, commit_result: str, verify_outcome: str) -> tuple[str, str]:
    if settle_outcome != "success":
        return "failed", FAIL
    if commit_result not in ("pushed", "nothing"):
        return "not_committed", FAIL
    if verify_outcome != "success":
        return "unverified", FAIL
    return ("settled_committed" if commit_result == "pushed" else "verified_idempotent"), SUCCESS


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def main(argv=None) -> int:
    mode = (argv or sys.argv[1:] or [""])[0]
    if mode == "snapshot":
        st = snapshot_state(heartbeat=_env("HB_SLUG"), gate_kind=_env("GATE_KIND"), claim_state=_env("CLAIM_STATE"),
                            claim_outcome=_env("CLAIM_OUTCOME"), odds_outcome=_env("ODDS_OUTCOME"),
                            skip_reason=_env("SKIP_REASON"), raw_file=_env("RAW_FILE"), verified=_env("VERIFIED"),
                            job_status=_env("JOB_STATUS"))
    elif mode == "pregame":
        st = pregame_state(claim_state=_env("CLAIM_STATE"), claim_outcome=_env("CLAIM_OUTCOME"),
                           odds_outcome=_env("ODDS_OUTCOME"), skip_reason=_env("SKIP_REASON"), raw_file=_env("RAW_FILE"),
                           verified=_env("VERIFIED"), job_status=_env("JOB_STATUS"))
    elif mode == "settle":
        st = settle_state(settle_outcome=_env("SETTLE_OUTCOME"), commit_result=_env("COMMIT_RESULT"),
                          verify_outcome=_env("VERIFY_OUTCOME"))
    else:
        print("failed fail")
        return 2
    print(f"{st[0]} {st[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
