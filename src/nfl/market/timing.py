"""Timezone-safe timing logic for Phase 4 (standard library only).

All wall-clock reasoning uses America/New_York via zoneinfo, so EDT/EST is automatic; there is
no fixed UTC-4 / UTC-5 arithmetic anywhere. Timestamps are stored in UTC.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
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


def resolve_kickoff(latest_observed_commence: datetime | None, nflverse_kickoff: datetime | None) -> datetime | None:
    """Conservative kickoff: the EARLIER of the latest Odds API commence_time and nflverse's
    scheduled kickoff, so a moved/flexed game can never let a post-kickoff snapshot count as pregame."""
    cands = [parse_utc(x) for x in (latest_observed_commence, nflverse_kickoff) if x is not None]
    return min(cands) if cands else None


def close_quality(mtk: float | None, n_books: int | None) -> bool:
    return (mtk is not None and n_books is not None and 0 < mtk <= C.CLOSE_QUALITY_MAX_MIN
            and n_books >= C.CLOSE_QUALITY_MIN_BOOKS)


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


def scheduled_gate(now: datetime, existing_raw_names: list[str], kinds: tuple[str, ...] | None = None) -> tuple[str | None, str]:
    """Which scheduled kind (if any) this run should take. Each cron fires twice (EDT and EST UTC
    hours); only the run at or within SCHEDULE_GATE_TOLERANCE_MIN after the ET target proceeds, and only
    if that kind has not already been captured for that target (dedupe). Returns (kind or None, reason)."""
    now = parse_utc(now)
    existing = [p for p in (parse_raw_name(n) for n in existing_raw_names) if p]
    for kind in (kinds or tuple(C.SCHEDULED_KINDS)):
        tgt = _latest_target(kind, now)
        lag = (now - tgt).total_seconds() / 60.0
        if not 0 <= lag <= C.SCHEDULE_GATE_TOLERANCE_MIN:
            continue
        if any(k == kind and ts >= tgt for ts, k in existing):
            return None, f"{kind} already captured for target {tgt.isoformat()}"
        return kind, f"{kind} target {to_et(tgt).isoformat()} lag {lag:.0f} min"
    return None, f"no scheduled target within {C.SCHEDULE_GATE_TOLERANCE_MIN} min (ET now {to_et(now).isoformat()})"


# ---- Pregame planner ------------------------------------------------------------------------

@dataclass
class PregameDecision:
    pull: bool
    reason: str
    target_kickoffs: list[str] = field(default_factory=list)


def plan_pregame(now: datetime, kickoffs: list[datetime], existing_raw_names: list[str],
                 credits_used_this_week: int | None = None) -> PregameDecision:
    """Decide whether to spend one credit now. kickoffs = commence times of listed events
    (from the free /events endpoint). Existing snapshots of ANY kind count as coverage."""
    now = parse_utc(now)
    if any(n.rsplit("/", 1)[-1].startswith("HALT_") for n in existing_raw_names):
        return PregameDecision(False, "halted: a HALT file exists in data/odds/raw")
    snaps = [ts for ts, _ in (p for p in (parse_raw_name(n) for n in existing_raw_names) if p)]
    slots = sorted({parse_utc(k) for k in kickoffs})
    wanted = []
    for k in slots:
        mtk = minutes_to_kickoff(now, k)
        if not (C.PREGAME_LATE_MIN <= mtk <= C.PREGAME_WINDOW_MAX):
            continue
        covered = any(C.PREGAME_WINDOW_MIN <= minutes_to_kickoff(ts, k) <= C.PREGAME_WINDOW_MAX for ts in snaps)
        if covered:
            continue
        attempts = sum(1 for ts in snaps if 0 < minutes_to_kickoff(ts, k) <= C.PREGAME_ATTEMPT_LOOKBACK_MIN)
        if attempts >= C.PREGAME_MAX_ATTEMPTS_PER_SLOT:
            continue
        if mtk < C.PREGAME_WINDOW_MIN and attempts >= 1:
            continue  # the single late attempt is only for a slot nothing has tried yet
        wanted.append(k)
    if not wanted:
        return PregameDecision(False, "no uncovered kickoff in the pregame window")
    if credits_used_this_week is not None and credits_used_this_week >= C.WEEKLY_CREDIT_CAP:
        return PregameDecision(False, f"weekly credit cap {C.WEEKLY_CREDIT_CAP} reached",
                               [k.isoformat() for k in wanted])
    return PregameDecision(True, f"{len(wanted)} kickoff slot(s) in window",
                           [k.isoformat() for k in wanted])


def pulls_this_week(now: datetime, existing_raw_names: list[str]) -> int:
    start = nfl_week_start(now)
    return sum(1 for p in (parse_raw_name(n) for n in existing_raw_names)
               if p and p[0] >= start and p[1] in C.ALL_KINDS and p[1] != "legacy_current")
