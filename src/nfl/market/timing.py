"""Timezone-safe timing logic for Phase 4 (standard library only).

All wall-clock reasoning uses America/New_York via zoneinfo, so EDT/EST is automatic; there is
no fixed UTC-4 / UTC-5 arithmetic anywhere. Timestamps are stored in UTC.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from src.nfl.market import phase4_config as C

ET = ZoneInfo(C.ET_ZONE)
UTC = timezone.utc
SNAPSHOT_TS_FMT = "%Y%m%dT%H%M%SZ"
RAW_NAME_RE = re.compile(r"^totals_(\d{8}T\d{6}Z)_([a-z_]+?)(?:-dup\d+)?\.json$")


def parse_utc(value) -> datetime:
    """ISO string ('...Z' or offset), snapshot id timestamp, or aware datetime -> aware UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed")
        return value.astimezone(UTC)
    s = str(value)
    if re.fullmatch(r"\d{8}T\d{6}Z", s):
        return datetime.strptime(s, SNAPSHOT_TS_FMT).replace(tzinfo=UTC)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {s}")
    return dt.astimezone(UTC)


def snapshot_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(SNAPSHOT_TS_FMT)


def to_et(dt: datetime) -> datetime:
    return parse_utc(dt).astimezone(ET)


def et_date(dt: datetime) -> date:
    return to_et(dt).date()


def nflverse_kickoff_utc(gameday: str, gametime: str | None) -> datetime | None:
    """nflverse gameday/gametime are US Eastern wall-clock values."""
    if not gameday or not gametime or str(gametime) in ("nan", "None"):
        return None
    d = date.fromisoformat(str(gameday)[:10])
    hh, mm = (int(x) for x in str(gametime).split(":")[:2])
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


def minutes_to_kickoff(snapshot_dt: datetime, kickoff_dt: datetime) -> float:
    return (parse_utc(kickoff_dt) - parse_utc(snapshot_dt)).total_seconds() / 60.0


def is_pregame(snapshot_dt: datetime, kickoff_dt: datetime) -> bool:
    """Strictly before kickoff. A snapshot at or after kickoff is never pregame."""
    return parse_utc(snapshot_dt) < parse_utc(kickoff_dt)


KICKOFF_RESOLVED = "resolved"
KICKOFF_UNRESOLVED = "unresolved"


def resolve_game_kickoff(odds_latest: datetime | None, nflverse: datetime | None) -> dict:
    """Kickoff policy for conflicting or changed kickoff times (documented in
    docs/phase4/VALIDATION_AND_NOTES.md, "Kickoff policy").

    Sources: odds_latest = the commence_time reported by the MOST RECENT Odds API snapshot that lists
    the game; nflverse = the scheduled kickoff (US Eastern wall clock, converted with zoneinfo) from the
    current schedule.
      - both present and within KICKOFF_AGREE_TOL_MIN: kickoff verified -> the earlier of the two,
        source "agreed", status "resolved". A legitimately postponed or flexed game lands here once both
        sources show the new time, so its valid pregame observations are kept.
      - both present but different: source "conflict", status "unresolved", kickoff = the EARLIER time
        (never classify a possibly post-kickoff observation as pregame).
      - only one source: source "odds_api_only" / "nflverse_only", status "unresolved" (no independent
        verification).
    close_quality is refused for any game whose status is not "resolved"."""
    a = parse_utc(odds_latest) if odds_latest is not None else None
    b = parse_utc(nflverse) if nflverse is not None else None
    if a is None and b is None:
        return {"kickoff": None, "source": "none", "status": KICKOFF_UNRESOLVED, "conflict_minutes": None}
    if a is None or b is None:
        k = a or b
        return {"kickoff": k, "source": "odds_api_only" if a else "nflverse_only", "status": KICKOFF_UNRESOLVED,
                "conflict_minutes": None}
    diff = abs((a - b).total_seconds()) / 60.0
    if diff <= C.KICKOFF_AGREE_TOL_MIN:
        return {"kickoff": min(a, b), "source": "agreed", "status": KICKOFF_RESOLVED, "conflict_minutes": round(diff, 1)}
    return {"kickoff": min(a, b), "source": "conflict", "status": KICKOFF_UNRESOLVED, "conflict_minutes": round(diff, 1)}


def resolve_kickoff(latest_observed_commence: datetime | None, nflverse_kickoff: datetime | None) -> datetime | None:
    """Kickoff time only (see resolve_game_kickoff for source and status)."""
    return resolve_game_kickoff(latest_observed_commence, nflverse_kickoff)["kickoff"]


def close_quality(mtk: float | None, n_books: int | None, kickoff_status: str) -> bool:
    """Taken 0-30 min before kickoff (strictly before) with >= 5 books AND a resolved kickoff."""
    return (kickoff_status == KICKOFF_RESOLVED and mtk is not None and n_books is not None
            and 0 < mtk <= C.CLOSE_QUALITY_MAX_MIN and n_books >= C.CLOSE_QUALITY_MIN_BOOKS)


def nfl_week_start(now: datetime) -> datetime:
    """Most recent Tuesday 00:00 ET at or before now (the credit-cap week)."""
    local = to_et(now)
    days_back = (local.weekday() - 1) % 7
    start_local = datetime.combine(local.date() - timedelta(days=days_back), time(0, 0), tzinfo=ET)
    return start_local.astimezone(UTC)


def parse_raw_name(name: str) -> tuple[datetime, str] | None:
    m = RAW_NAME_RE.match(name.rsplit("/", 1)[-1])
    if not m:
        return None
    return parse_utc(m.group(1)), m.group(2)


# ---- Scheduled-snapshot ET gate -------------------------------------------------------------

def _latest_target(kind: str, now: datetime) -> datetime:
    wd, hh, mm = C.SCHEDULED_KINDS[kind]
    local = to_et(now)
    for back in range(0, 8):
        d = local.date() - timedelta(days=back)
        if d.weekday() != wd:
            continue
        tgt = datetime.combine(d, time(hh, mm), tzinfo=ET)
        if tgt <= local:
            return tgt.astimezone(UTC)
    raise AssertionError("unreachable")


class GateDecision(NamedTuple):
    """kind is None when nothing should be captured. Indexable like the old (kind, reason) tuple."""
    kind: str | None
    reason: str
    target: datetime | None = None
    delay_minutes: float | None = None
    window_end: datetime | None = None
    window_kind: str | None = None   # the kind whose window is live (captured now OR already captured)

    @property
    def delayed(self) -> bool:
        return self.delay_minutes is not None and self.delay_minutes > C.DELAYED_THRESHOLD_MIN


def next_scheduled_target(after: datetime) -> datetime:
    """Earliest scheduled target of ANY kind strictly after `after`."""
    after = parse_utc(after)
    local = to_et(after)
    best = None
    for kind, (wd, hh, mm) in C.SCHEDULED_KINDS.items():
        for fwd in range(0, 9):
            d = local.date() + timedelta(days=fwd)
            if d.weekday() != wd:
                continue
            tgt = datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)
            if tgt > after:
                best = tgt if best is None or tgt < best else best
                break
    return best


def late_window(kind: str, target: datetime) -> datetime:
    """A delayed run of `kind` may still capture until min(target + 18 h, next scheduled target)."""
    return min(parse_utc(target) + timedelta(minutes=C.SCHEDULE_LATE_MAX_MIN), next_scheduled_target(target))


def _captured(existing, kind: str, start: datetime, end: datetime) -> bool:
    return any(k == kind and start <= ts < end for ts, k in existing)


def scheduled_gate(now: datetime, existing_raw_names: list[str], kinds: tuple[str, ...] | None = None) -> GateDecision:
    """Which scheduled kind (if any) this run should capture, for on-time AND delayed runs.

    For each kind, its latest ET target T (zoneinfo, DST-automatic) is live from T until
    late_window(kind, T) = min(T + 18 h, next scheduled target of any kind). A run inside that window
    captures unless that kind already has a snapshot for T (dedupe across GitHub cron, the twin cron,
    external dispatch and manual runs). A run after the window closes captures nothing for T (obsolete).
    The capture keeps its real timestamp; T and the delay are returned so they are recorded separately."""
    now = parse_utc(now)
    existing = [p for p in (parse_raw_name(n) for n in existing_raw_names) if p]
    obsolete = []
    for kind in (kinds or tuple(C.SCHEDULED_KINDS)):
        tgt = _latest_target(kind, now)
        end = late_window(kind, tgt)
        delay = round((now - tgt).total_seconds() / 60.0, 1)
        if now >= end:
            obsolete.append(kind)
            continue
        if _captured(existing, kind, tgt, end):
            return GateDecision(None, f"{kind} already captured for target {to_et(tgt).isoformat()}", tgt, delay, end, kind)
        label = "delayed" if delay > C.DELAYED_THRESHOLD_MIN else "on time"
        return GateDecision(kind, f"{kind} target {to_et(tgt).isoformat()} ({label}, {delay:.0f} min after target)",
                            tgt, delay, end, kind)
    return GateDecision(None, f"no live scheduled window (ET now {to_et(now).isoformat()}; obsolete: {', '.join(obsolete) or 'none'})")


def forced_target(kind: str, now: datetime) -> GateDecision:
    """For an explicitly chosen kind (manual dispatch): the kind's latest target and the real delay."""
    now = parse_utc(now)
    tgt = _latest_target(kind, now)
    return GateDecision(kind, f"{kind} forced; latest target {to_et(tgt).isoformat()}", tgt,
                        round((now - tgt).total_seconds() / 60.0, 1), late_window(kind, tgt), kind)


# ---- Pregame planner ------------------------------------------------------------------------

@dataclass
class PregameDecision:
    pull: bool
    reason: str
    targets: list[dict] = field(default_factory=list)   # [{"event_id": ..., "kickoff": iso}]

    @property
    def target_kickoffs(self) -> list[str]:
        return sorted({t["kickoff"] for t in self.targets})


def coverage_from_quote_csvs(paths) -> dict[str, list[tuple[datetime, int]]]:
    """Per-game coverage from paired-quote files (standard library only).

    Returns {event_id: [(snapshot_dt, n_books_with_a_valid_paired_quote), ...]}. Quote files only ever
    contain valid two-way totals (same book, same number on both sides), so one row = one book that
    really quoted that game in that snapshot. A snapshot that exists but did not quote a game gives
    that game no coverage."""
    import csv
    counts: dict[tuple[str, str], int] = {}
    for p in paths:
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                eid, ts = row.get("event_id"), row.get("snapshot_ts_utc")
                if not eid or not ts or row.get("total_line") in (None, ""):
                    continue
                if row.get("over_price") in (None, "") or row.get("under_price") in (None, ""):
                    continue
                counts[(eid, ts)] = counts.get((eid, ts), 0) + 1
    out: dict[str, list[tuple[datetime, int]]] = {}
    for (eid, ts), n in counts.items():
        out.setdefault(eid, []).append((parse_utc(ts), n))
    return out


def quote_files(odds_dir) -> list:
    """All paired-quote files under an odds dir: the per-snapshot files plus the one combined
    validation file (data/odds/quotes_v2.csv)."""
    from pathlib import Path
    d = Path(odds_dir)
    files = sorted((d / "quotes").glob("quotes_*.csv")) if (d / "quotes").exists() else []
    return ([d / "quotes_v2.csv"] if (d / "quotes_v2.csv").exists() else []) + files


def _scheduled_due(now: datetime, kickoff: datetime, existing_parsed) -> bool:
    """True while a scheduled kind whose ET target falls inside this kickoff's T-30..T-10 window is still
    expected: now is before target + PREGAME_SCHEDULED_DEFER_MIN and that kind has no capture yet."""
    for kind in C.SCHEDULED_KINDS:
        tgt = _latest_target(kind, kickoff)
        if not (C.PREGAME_WINDOW_MIN <= minutes_to_kickoff(tgt, kickoff) <= C.PREGAME_WINDOW_MAX):
            continue
        if now >= tgt + timedelta(minutes=C.PREGAME_SCHEDULED_DEFER_MIN):
            continue
        if any(k == kind and ts >= tgt for ts, k in existing_parsed):
            continue
        return True
    return False


def plan_pregame(now: datetime, events: list[tuple[str, datetime]], existing_raw_names: list[str],
                 credits_used_this_week: int | None = None,
                 coverage: dict[str, list[tuple[datetime, int]]] | None = None) -> PregameDecision:
    """Decide whether to spend one credit now, GAME BY GAME.

    events   = [(event_id, kickoff)] from the free /events endpoint (current kickoff times).
    coverage = coverage_from_quote_csvs(...): which games actually received a valid paired quote in
               which snapshot.
    A game is covered only if some snapshot taken 10-30 minutes before ITS kickoff contains at least
    one valid paired quote FOR THAT GAME (event_id). A snapshot that exists near kickoff but did not
    quote the game does not cover it. Partial book coverage still counts as covered (re-pulling does
    not add books that are not posting); close_quality separately requires 5+ books.
    Credit bounds per game: at most 2 snapshots within 40 minutes before its kickoff (any snapshot
    counts as an attempt, quoted or not), one late attempt down to T-5 only if nothing has tried yet,
    never at or after kickoff, weekly cap, and any HALT file stops everything."""
    now = parse_utc(now)
    coverage = coverage or {}
    if any(n.rsplit("/", 1)[-1].startswith("HALT_") for n in existing_raw_names):
        return PregameDecision(False, "halted: a HALT file exists in data/odds/raw")
    existing_parsed = [p for p in (parse_raw_name(n) for n in existing_raw_names) if p]
    snaps = [ts for ts, _ in existing_parsed]
    wanted, deferred = [], []
    for eid, k in sorted({(str(e), parse_utc(k)) for e, k in events}, key=lambda x: (x[1], x[0])):
        mtk = minutes_to_kickoff(now, k)
        if not (C.PREGAME_LATE_MIN <= mtk <= C.PREGAME_WINDOW_MAX):
            continue
        covered = any(n >= 1 and C.PREGAME_WINDOW_MIN <= minutes_to_kickoff(ts, k) <= C.PREGAME_WINDOW_MAX
                      for ts, n in coverage.get(eid, []))
        if covered:
            continue
        attempts = sum(1 for ts in snaps if 0 < minutes_to_kickoff(ts, k) <= C.PREGAME_ATTEMPT_LOOKBACK_MIN)
        if attempts >= C.PREGAME_MAX_ATTEMPTS_PER_SLOT:
            continue
        if mtk < C.PREGAME_WINDOW_MIN and attempts >= 1:
            continue  # the single late attempt is only for a game nothing has tried yet
        if _scheduled_due(now, k, existing_parsed):
            deferred.append(eid)
            continue
        wanted.append({"event_id": eid, "kickoff": k.isoformat()})
    if not wanted:
        if deferred:
            return PregameDecision(False, f"waiting for a scheduled snapshot due in the window of {len(deferred)} game(s)")
        return PregameDecision(False, "no uncovered game in the pregame window")
    if credits_used_this_week is not None and credits_used_this_week >= C.WEEKLY_CREDIT_CAP:
        return PregameDecision(False, f"weekly credit cap {C.WEEKLY_CREDIT_CAP} reached", wanted)
    return PregameDecision(True, f"{len(wanted)} uncovered game(s) in window", wanted)


def pulls_this_week(now: datetime, existing_raw_names: list[str]) -> int:
    start = nfl_week_start(now)
    return sum(1 for p in (parse_raw_name(n) for n in existing_raw_names)
               if p and p[0] >= start and p[1] in C.ALL_KINDS and p[1] != "legacy_current")
