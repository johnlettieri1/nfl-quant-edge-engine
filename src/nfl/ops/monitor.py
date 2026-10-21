"""Collection monitor (scheduling recovery, approved Oct 2026).

Reads the records branch and recent workflow results and reports what SHOULD have happened but did not.
A dispatch accepted by GitHub is not proof of collection, so every check looks at the records themselves:

  missed-snapshot   a scheduled kind has no capture by the time its late window closed
  missed-pregame    a kickoff slot whose games have no paired quote in the T-30..kickoff window
  unsettled         a predicted game, verified final, still missing from both ledgers 36 h after kickoff
  halt              a HALT file exists (cost anomaly; all pulls refused)
  workflow-failure  a weekly / pregame / settle / monitor run failed in the last 26 h
  records-stale     in season, no records commit for 50 h

Alerts become GitHub issues labeled `collection-alert`, one issue per finding key, never duplicated (an
issue is not re-opened or re-created once it exists, even if closed). The monitor is itself watched by an
external heartbeat (healthchecks.io) so a monitor that never starts is reported outside GitHub.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.nfl.market import phase4_config as C
from src.nfl.market.timing import (_latest_target, late_window, nflverse_kickoff_utc, parse_raw_name,
                                   parse_utc, to_et)

FAIL_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}
WATCHED_WORKFLOWS = {"Weekly totals", "Pregame planner", "Settlement", "Collection monitor"}


def _finding(key: str, title: str, body: str) -> dict:
    return {"key": key, "title": f"[{C.ALERT_LABEL}] {title}", "body": body}


def _start(now: datetime, start: datetime | None) -> datetime:
    floor = now - timedelta(days=C.MONITOR_LOOKBACK_DAYS)
    return max(floor, start or parse_utc(C.MONITOR_START_UTC))


def missed_scheduled(now: datetime, raw_names: list[str], start: datetime | None = None) -> list[dict]:
    now, begin = parse_utc(now), _start(parse_utc(now), start)
    existing = [p for p in (parse_raw_name(n) for n in raw_names) if p]
    out = []
    for kind in C.SCHEDULED_KINDS:
        t = now
        seen = set()
        for _ in range(C.MONITOR_LOOKBACK_DAYS // 7 + 2):
            tgt = _latest_target(kind, t)
            t = tgt - timedelta(minutes=1)
            if tgt in seen or tgt < begin:
                break
            seen.add(tgt)
            end = late_window(kind, tgt)
            if now < end:
                continue  # still live: not missed yet
            if not any(k == kind and tgt <= ts < end for ts, k in existing):
                d = to_et(tgt)
                out.append(_finding(f"missed-snapshot-{kind}-{d:%Y%m%d}",
                                    f"Missed scheduled snapshot {kind} {d:%a %b %d %I:%M %p} ET",
                                    f"No `{kind}` capture between its target {d.isoformat()} and the end of its late window "
                                    f"{to_et(end).isoformat()}. Check the Weekly totals runs and the external scheduler."))
    return out


def missed_pregame(now: datetime, quotes: pd.DataFrame, schedules: pd.DataFrame, start: datetime | None = None) -> list[dict]:
    """A game is covered when some snapshot strictly before its kickoff and at most 30 min before it holds
    a valid paired quote for that game. Missed games are grouped per kickoff slot."""
    now, begin = parse_utc(now), _start(parse_utc(now), start)
    s = schedules[schedules.season >= C.FORWARD_SEASON_MIN]
    have = {}
    if quotes is not None and len(quotes):
        for gid, ts in zip(quotes.game_id, quotes.snapshot_ts_utc):
            if isinstance(gid, str):
                have.setdefault(gid, []).append(parse_utc(ts))
    slots: dict[datetime, list[str]] = {}
    for r in s.itertuples():
        k = nflverse_kickoff_utc(str(r.gameday)[:10], None if pd.isna(r.gametime) else str(r.gametime))
        if k is None or k < begin or k > now - timedelta(minutes=2):
            continue
        ok = any(k - timedelta(minutes=C.CLOSE_QUALITY_MAX_MIN) <= ts < k for ts in have.get(r.game_id, []))
        if not ok:
            slots.setdefault(k, []).append(r.game_id)
    out = []
    for k, games in sorted(slots.items()):
        d = to_et(k)
        out.append(_finding(f"missed-pregame-{d:%Y%m%d-%H%M}", f"Missed pregame window {d:%a %b %d %I:%M %p} ET",
                            "No paired quote in the T-30 to kickoff window for: " + ", ".join(sorted(games))
                            + ". Check the Pregame planner runs and the external scheduler."))
    return out


def settle_reference_kickoff(row):
    from src.nfl.reporting.ledger import settle_reference_kickoff as _k
    return _k(row)


def unsettled(now: datetime, schedules: pd.DataFrame, pred_dir: Path, ledgers: list[Path]) -> list[dict]:
    now = parse_utc(now)
    predicted = set()
    for p in sorted(pred_dir.glob("totals_*.json")):
        if p.name == "totals_index.json":
            continue
        for g in json.loads(p.read_text()).get("games", []):
            predicted.add(g["game_id"])
    settled = set()
    for led in ledgers:
        if led.exists():
            settled |= set(pd.read_csv(led, usecols=["game_id"]).game_id.astype(str))
    late = []
    for r in schedules[schedules.game_id.isin(predicted)].itertuples():
        if pd.isna(r.home_score) or pd.isna(r.away_score):
            continue
        k = settle_reference_kickoff({"gameday": r.gameday, "gametime": r.gametime})
        if k is not None and now >= k + timedelta(hours=C.SETTLE_ALERT_HOURS) and r.game_id not in settled:
            late.append(r.game_id)
    out = []
    for week in sorted({g[:7] for g in late}):
        games = sorted(g for g in late if g[:7] == week)
        out.append(_finding(f"unsettled-{week}", f"Settlement missing for week {week}",
                            "Final, predicted games not in either ledger 36 h after kickoff: " + ", ".join(games)
                            + ". Run the Settlement workflow (no credits)."))
    return out


def halt(raw_names: list[str]) -> list[dict]:
    return [_finding(f"halt-{n}", f"Collection HALTED: {n}",
                     "A pull cost more than the approved 1 credit. All paid pulls are refused until John approves "
                     "removing this file. Settlement and monitoring continue.")
            for n in raw_names if n.rsplit("/", 1)[-1].startswith("HALT_")]


def workflow_failures(now: datetime, runs: list[dict]) -> list[dict]:
    now, out = parse_utc(now), []
    for r in runs:
        if r.get("workflowName", r.get("name")) not in WATCHED_WORKFLOWS:
            continue
        created = parse_utc(r["createdAt"]) if r.get("createdAt") else None
        if created is None or now - created > timedelta(hours=26) or r.get("conclusion") not in FAIL_CONCLUSIONS:
            continue
        name = r.get("workflowName", r.get("name"))
        out.append(_finding(f"workflow-failure-{r.get('databaseId', r.get('id'))}",
                            f"Workflow failed: {name} at {to_et(created):%a %b %d %I:%M %p} ET",
                            f"Conclusion `{r.get('conclusion')}`. {r.get('url', '')} "
                            "A failure after a paid pull can mean records were not committed: check the run."))
    return out


def records_stale(now: datetime, head_time: datetime | None) -> list[dict]:
    if head_time is None:
        return []
    now, head_time = parse_utc(now), parse_utc(head_time)
    local = to_et(now)
    in_season = local.month in (9, 10, 11, 12, 1) or (local.month == 2 and local.day <= 20)
    if not in_season or now - head_time <= timedelta(hours=C.RECORDS_STALE_HOURS):
        return []
    h = to_et(head_time)
    return [_finding(f"records-stale-{h:%Y%m%dT%H%M}", f"No records commit since {h:%a %b %d %I:%M %p} ET",
                     f"Last records commit {h.isoformat()}, {(now - head_time).total_seconds() / 3600:.0f} h ago "
                     f"(alert threshold {C.RECORDS_STALE_HOURS} h). Collection or records commits may be failing.")]


def run_checks(now: datetime, records: Path, schedules: pd.DataFrame, runs: list[dict],
               head_time: datetime | None, start: datetime | None = None) -> list[dict]:
    from src.nfl.data.odds_snapshot import load_quotes
    raw_dir = records / "data" / "odds" / "raw"
    names = sorted(p.name for p in raw_dir.iterdir()) if raw_dir.exists() else []
    pred = records / "data" / "predictions"
    quotes = load_quotes(records / "data" / "odds")
    return (halt(names) + missed_scheduled(now, names, start) + missed_pregame(now, quotes, schedules, start)
            + unsettled(now, schedules, pred, [pred / "totals_ledger_v2.csv", pred / "totals_ledger_legacy_pre_spec.csv"])
            + workflow_failures(now, runs) + records_stale(now, head_time))


def sync_issues(findings: list[dict], gh=None) -> list[str]:
    """Create one issue per new finding key. Existing issues with the same title (open OR closed) are
    left alone, so a known problem never produces repeated issues."""
    def _gh(*args: str) -> str:
        return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    gh = gh or _gh
    if not findings:
        return []
    gh("label", "create", C.ALERT_LABEL, "--color", "B60205", "--description", "Phase 4 collection monitor", "--force")
    existing = {i["title"] for i in json.loads(gh("issue", "list", "--label", C.ALERT_LABEL, "--state", "all",
                                                  "--limit", "500", "--json", "title") or "[]")}
    created = []
    for f in findings:
        if f["title"] in existing:
            continue
        gh("issue", "create", "--title", f["title"], "--body", f["body"], "--label", C.ALERT_LABEL)
        existing.add(f["title"])
        created.append(f["key"])
    return created
