"""Paid-pull claims: an optimistic compare-and-swap on the records branch (Safeguard 3, Oct 2026).

Why: the weekly and pregame workflows run in different concurrency groups (a shared group would let a new
pending run cancel an older pending one and lose a legitimate capture). Each job checks the records before
paying, but two jobs can pass that check before either commits. The race is real for:
  - a scheduled full-slate snapshot and a pregame pull serving the same game window (9:30 AM ET games);
  - any two jobs whose records push is delayed or fails.

How: immediately before paying, a job records its intent as a small NEW file under data/odds/claims/ and
pushes it to `records` WITHOUT rebasing. A git push only succeeds as a fast-forward of the commit the job
based its decision on, so of two jobs that decided from the same records state, only one claim lands. The
loser fetches, sees the winner's claim (or capture), re-decides, and does not pay. A job pays only after its
own claim push succeeded; if it cannot record a claim it does not pay (fail closed).

No lock is ever held or waited on: a claim is just a record that expires after CLAIM_TTL_MIN. There is no
deadlock and no manual unlock. After the TTL, a claim that produced no capture no longer blocks anyone.

What this does NOT guarantee (documented in docs/phase4/SCHEDULING_RECOVERY_SPEC.md): exactly-once paid
collection. Duplicates remain possible when a paying job fails to commit its capture and another job runs
after the TTL, when a job takes longer than the TTL between claim and commit, when a delayed scheduled
snapshot and a pregame pull legitimately serve different purposes in the same window, and for manual runs.

Standard library only.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from src.nfl.market import phase4_config as C
from src.nfl.market.timing import (coverage_from_quote_csvs, late_window, minutes_to_kickoff, parse_raw_name,
                                   parse_utc, plan_pregame, quote_files, snapshot_ts)

CLAIM_SUBDIR = Path("data/odds/claims")
CLAIM_RE = re.compile(r"^claim_(\d{8}T\d{6}Z)_([a-z_]+)_r([A-Za-z0-9-]+)\.json$")
SCHEMA = "paid_pull_claim_v1"
GIT_ID = ["-c", "user.name=claim-bot", "-c", "user.email=claim-bot@users.noreply.github.com"]


@dataclass
class ClaimDecision:
    pay: bool
    state: str            # pay | already_captured | claimed_elsewhere | obsolete | not_needed | claim_failed
    reason: str
    targets: list[dict] = field(default_factory=list)
    claim_file: str | None = None


# ---- claim records ---------------------------------------------------------------------------------

def claim_filename(now: datetime, kind: str, run_id: str) -> str:
    rid = re.sub(r"[^A-Za-z0-9-]", "-", str(run_id or "local"))
    return f"claim_{snapshot_ts(parse_utc(now))}_{kind}_r{rid}.json"


def load_claims(odds_dir: Path) -> list[dict]:
    d = Path(odds_dir) / "claims"
    out = []
    for p in sorted(d.glob("claim_*.json")) if d.exists() else []:
        if not CLAIM_RE.match(p.name):
            continue
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        c["_file"] = p.name
        c["_ts"] = parse_utc(c["claim_ts"])
        out.append(c)
    return out


def _raw(odds_dir: Path) -> list[tuple[datetime, str]]:
    d = Path(odds_dir) / "raw"
    names = sorted(p.name for p in d.iterdir()) if d.exists() else []
    return [p for p in (parse_raw_name(n) for n in names) if p]


def resolved(claim: dict, raw: list[tuple[datetime, str]]) -> bool:
    """A claim is resolved once a capture of its kind exists inside [claim_ts, claim_ts + TTL]."""
    end = claim["_ts"] + timedelta(minutes=C.CLAIM_TTL_MIN)
    return any(k == claim["kind"] and claim["_ts"] <= ts <= end for ts, k in raw)


def active_claims(claims: list[dict], raw: list[tuple[datetime, str]], now: datetime) -> list[dict]:
    """Unresolved claims made within the last CLAIM_TTL_MIN minutes. A job never sees its own claim here:
    it decides before writing it, and a rejected claim commit is discarded by the next fetch/reset."""
    now = parse_utc(now)
    return [c for c in claims
            if timedelta(0) <= now - c["_ts"] < timedelta(minutes=C.CLAIM_TTL_MIN) and not resolved(c, raw)]


# ---- decisions (pure; read the records checkout) ---------------------------------------------------

def decide_scheduled(now: datetime, kind: str, target: datetime, odds_dir: Path, trigger: str) -> ClaimDecision:
    now, target = parse_utc(now), parse_utc(target)
    raw = _raw(odds_dir)
    end = late_window(kind, target)
    if any(k == kind and target <= ts < end for ts, k in raw):
        return ClaimDecision(False, "already_captured", f"{kind} already captured for target {target.isoformat()}")
    if now >= end and trigger != "manual":
        return ClaimDecision(False, "obsolete", f"{kind} window for {target.isoformat()} closed at {end.isoformat()}")
    for c in active_claims(load_claims(odds_dir), raw, now):
        if c["kind"] == kind and parse_utc(c["scheduled_target"]) == target:
            return ClaimDecision(False, "claimed_elsewhere", f"run {c.get('run_id')} claimed {kind} at {c['claim_ts']}")
    return ClaimDecision(True, "pay", f"{kind} target {target.isoformat()} unclaimed and uncaptured")


def decide_pregame(now: datetime, targets: list[dict], odds_dir: Path) -> ClaimDecision:
    now = parse_utc(now)
    names = sorted(p.name for p in (Path(odds_dir) / "raw").iterdir()) if (Path(odds_dir) / "raw").exists() else []
    raw = [p for p in (parse_raw_name(n) for n in names) if p]
    events = [(t["event_id"], parse_utc(t["kickoff"])) for t in targets or []]
    d = plan_pregame(now, events, names, coverage=coverage_from_quote_csvs(quote_files(odds_dir)))
    if not d.pull:
        return ClaimDecision(False, "not_needed", d.reason)
    act = active_claims(load_claims(odds_dir), raw, now)
    keep = []
    for t in d.targets:
        k = parse_utc(t["kickoff"])
        if any(c["kind"] == "pregame_window" and t["event_id"] in (c.get("event_ids") or []) for c in act):
            continue
        # a scheduled full-slate capture claimed inside this game's window will quote it
        if any(c["kind"] in C.SCHEDULED_KINDS
               and C.PREGAME_WINDOW_MIN <= minutes_to_kickoff(c["_ts"], k) <= C.PREGAME_WINDOW_MAX for c in act):
            continue
        keep.append(t)
    if not keep:
        return ClaimDecision(False, "claimed_elsewhere", "every target game is claimed by another in-progress pull")
    return ClaimDecision(True, "pay", f"{len(keep)} game(s) unclaimed and uncovered", keep)


# ---- compare-and-swap on the records branch -------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=check, capture_output=True, text=True)


def claim_with_cas(repo: Path, decide: Callable[[Path], ClaimDecision], claim_body: Callable[[ClaimDecision], dict],
                   now_fn: Callable[[], datetime], run_id: str, kind: str, branch: str = "records",
                   max_attempts: int = 5, before_push: Callable[[], None] | None = None) -> ClaimDecision:
    """Fetch -> decide -> write claim -> push without rebase. Retry the whole cycle if the push is rejected.
    Returns the decision; pay is True only if this job's claim is now on the remote branch."""
    repo = Path(repo)
    for attempt in range(1, max_attempts + 1):
        if _git(repo, "fetch", "--quiet", "origin", branch, check=False).returncode != 0:
            continue
        _git(repo, "reset", "--quiet", "--hard", "FETCH_HEAD")
        d = decide(repo / "data" / "odds")
        if not d.pay:
            return d
        now = now_fn()
        name = claim_filename(now, kind, f"{run_id}-{attempt}")
        path = repo / CLAIM_SUBDIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"schema_version": SCHEMA, "claim_ts": parse_utc(now).isoformat(), "kind": kind, "run_id": f"{run_id}-{attempt}",
                "ttl_minutes": C.CLAIM_TTL_MIN, **claim_body(d),
                "note": "intent to make ONE paid odds pull; not a quote and not a capture"}
        with open(path, "x", encoding="utf-8") as f:
            json.dump(body, f, indent=1)
        _git(repo, "add", str(CLAIM_SUBDIR / name))
        bad = _guard(repo)
        if bad:   # never push anything the records guard would reject
            return ClaimDecision(False, "claim_failed", "records guard rejected the claim: " + "; ".join(bad))
        _git(repo, "commit", "--quiet", "-m", f"records: claim {kind} {name}")
        if before_push:
            before_push()
        if _git(repo, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", check=False).returncode == 0:
            d.claim_file = name
            return d
        # rejected (another job pushed first) or a network error: drop the local commit and re-decide
    return ClaimDecision(False, "claim_failed", f"could not record a claim after {max_attempts} attempts; not paying")


def _guard(repo: Path) -> list[str]:
    from src.nfl.ops.records_guard import evaluate
    lines = _git(repo, "diff", "--cached", "--name-status", "--no-renames").stdout.splitlines()
    changes = [(ln.split("\t")[0], ln.split("\t")[-1]) for ln in lines if ln.strip()]
    return evaluate(changes, lambda q: subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{q}"], capture_output=True).stdout,
                    lambda q: subprocess.run(["git", "-C", str(repo), "show", f":{q}"], capture_output=True).stdout)


def refresh_odds_dir(repo: Path, odds_dir: Path) -> None:
    """Copy the just-fetched records data/odds over the restored working copy (new files only appear)."""
    src = Path(repo) / "data" / "odds"
    if src.exists():
        shutil.copytree(src, odds_dir, dirs_exist_ok=True)
