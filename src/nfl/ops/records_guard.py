"""Records-branch guard (Phase 4): the logic that used to live inline in weekly.yml, now testable.

Run inside the records checkout after `git add -A`:
    python -m src.nfl.ops.records_guard --repo records

Rules:
  1. Every staged path must be under data/odds/, data/predictions/ or data/weather/.
  2. Added files are allowed (new snapshots, new envelopes).
  3. Append-only CSVs may only grow at the end (old bytes must be an exact prefix of new bytes).
  4. totals_index.json is the single mutable index.
  5. Everything else that already exists is immutable: no modification, no deletion.
  6. No staged content may contain an API key.
  7. No unpublished temp file (basename starting ".tmp-", left by an interrupted crash-safe write).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Callable

ALLOWED_PREFIXES = ("data/odds/", "data/predictions/", "data/weather/")
APPEND_ONLY = frozenset({
    "data/odds/book_lines.csv", "data/odds/game_summary.csv",           # frozen legacy (Phase 2B)
    "data/odds/quotes_v2.csv", "data/weather/forecasts_v1.csv",
    "data/predictions/totals_ledger.csv", "data/predictions/totals_ledger_v2.csv",
})
MUTABLE = frozenset({"data/predictions/totals_index.json"})


def evaluate(changes: list[tuple[str, str]], read_old: Callable[[str], bytes], read_new: Callable[[str], bytes],
             secrets: list[str] | None = None) -> list[str]:
    """changes = [(status, path)] with status A/M/D/R/C/T. Returns a list of violations (empty = pass)."""
    bad = []
    for status, path in changes:
        st = status[0]
        if not path.startswith(ALLOWED_PREFIXES):
            bad.append(f"path outside records data dirs: {path}")
            continue
        if path.rsplit("/", 1)[-1].startswith(".tmp-"):
            bad.append(f"unpublished temp file (interrupted write): {path}")
            continue
        if st == "A":
            pass
        elif st == "M" and path in MUTABLE:
            pass
        elif st == "M" and path in APPEND_ONLY:
            old, new = read_old(path), read_new(path)
            if not new.startswith(old):
                bad.append(f"append-only file rewritten: {path}")
        elif st == "D":
            bad.append(f"immutable record deleted: {path}")
        else:
            bad.append(f"immutable record modified ({status}): {path}")
        if st in ("A", "M"):
            data = read_new(path)
            if b"apiKey=" in data:
                bad.append(f"possible API key in {path}")
            for s in secrets or []:
                if s and s.encode() in data:
                    bad.append(f"secret value found in {path}")
    return bad


def _git(repo: str, *args: str) -> bytes:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    lines = _git(a.repo, "diff", "--cached", "--name-status", "--no-renames").decode().splitlines()
    changes = [(ln.split("\t")[0], ln.split("\t")[-1]) for ln in lines if ln.strip()]
    has_head = subprocess.run(["git", "-C", a.repo, "rev-parse", "--verify", "HEAD"], capture_output=True).returncode == 0
    read_old = (lambda p: _git(a.repo, "show", f"HEAD:{p}")) if has_head else (lambda p: b"")
    read_new = lambda p: _git(a.repo, "show", f":{p}")
    bad = evaluate(changes, read_old, read_new, secrets=[os.environ.get("ODDS_API_KEY", "")])
    for b in bad:
        print(f"::error title=records-guard::{b}")
    print(f"records guard: {len(changes)} staged change(s), {len(bad)} violation(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
