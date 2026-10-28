"""Per-book NFL totals snapshots from The Odds API (Phase 4, spec docs/phase4/PHASE_4_SPEC.md).

One paid call = one snapshot = 1 credit (totals market, the frozen 10-book list in
configs/phase4_books.yaml). Every snapshot is stored two ways, both append-only:

  data/odds/raw/totals_<UTC ts>_<kind>.json   envelope `odds_raw_v2`: request params (never the key),
                                              usage headers, code SHA, workflow run id, SHA-256 of the
                                              exact response bytes, and the response body itself.
                                              Created exclusively; never overwritten.
  data/odds/quotes/quotes_<snapshot_id>.csv   one row per (game, book) for that snapshot: ONE book, ONE
                                              total line, THAT book's Over and Under prices at that line.
                                              One new file per snapshot (exclusive create), so concurrent
                                              workflows never write the same file. (The single validation
                                              snapshot of Oct 15 2026 lives in the earlier combined file
                                              data/odds/quotes_v2.csv; readers load both.)

A pull that costs more than the approved 1 credit writes data/odds/raw/HALT_cost_anomaly_<ts>.json;
while any HALT file exists every later pull is refused (John's approval needed to clear it).

Terminology (spec section 8): snapshots carry a `snapshot_kind` describing what we did
(tue_scheduled, thu_scheduled, sat_scheduled, sun_scheduled, sun_night_scheduled, pregame_window,
manual; the one pre-spec file is legacy_current). Per-game roles (first_observed,
last_observed_pregame, close_quality) are computed in src/nfl/market, never stored as tags.
Nothing here is called an "opener" or a "closing line".

The legacy Phase 2B files data/odds/book_lines.csv and data/odds/game_summary.csv are frozen:
this module never writes them again (their rows stay untouched on the records branch).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.nfl.data.odds_api import fetch_balance, fetch_totals_raw
from src.nfl.data.team_mapping import to_nflverse_code
from src.nfl.market import phase4_config as C
from src.nfl.market.books import request_bookmakers
from src.nfl.market.timing import (ET, UTC, coverage_from_quote_csvs, et_date, is_pregame, minutes_to_kickoff,
                                   late_window, nflverse_kickoff_utc, parse_raw_name, parse_utc, plan_pregame,
                                   credits_this_week, quote_files, resolve_game_kickoff, snapshot_ts)

logger = logging.getLogger(__name__)

ODDS_DIR = Path("data/odds")
RAW_SCHEMA = "odds_raw_v2"
QUOTES_FILE = "quotes_v2.csv"          # legacy combined file (validation snapshot only)
QUOTES_DIR = "quotes"
SPREAD_QUOTES_DIR = "spread_quotes"    # Phase 5A: one paired-spread file per snapshot
HALT_PREFIX = "HALT_"
QUOTE_FIELDS = ["snapshot_id", "snapshot_ts_utc", "snapshot_kind", "event_id", "game_id", "match_method",
                "home_team", "away_team", "commence_time_utc_as_reported", "kickoff_nflverse_utc", "kickoff_utc_resolved",
                "kickoff_source", "kickoff_status", "kickoff_conflict_minutes", "kickoff_et",
                "minutes_to_kickoff", "is_pregame", "book", "total_line", "over_price", "under_price",
                "book_last_update", "quote_age_minutes", "raw_file", "response_sha256",
                # snapshot timing provenance (scheduling recovery): real capture time is snapshot_ts_utc;
                # the intended schedule target and delay are kept separately, never substituted for it
                "trigger", "scheduled_target_utc", "schedule_delay_minutes", "delayed"]
TIMING_FIELDS = ("trigger", "scheduled_target_utc", "schedule_delay_minutes", "delayed")


class BudgetError(RuntimeError):
    """A paid pull was refused before any credit was spent."""


class CostAnomalyError(RuntimeError):
    """The API charged more than the approved plan expects. The raw response is already saved."""


class SnapshotNotNeeded(RuntimeError):
    """Re-check right before paying found nothing to buy. No credit was spent."""


class PregameNotNeeded(SnapshotNotNeeded):
    """Re-check right before paying: every target kickoff is already covered, too close, or past."""


class ScheduledNotNeeded(SnapshotNotNeeded):
    """Re-check right before paying: this scheduled target already has a capture, or its window closed."""


# ---- matching -------------------------------------------------------------------------------

def match_game_id(home: str, away: str, commence_iso: str, schedules: pd.DataFrame | None) -> tuple[str | None, str]:
    """Match an Odds API event to a nflverse game_id by team codes and the kickoff's US Eastern
    calendar date (zoneinfo, DST-aware). Falls back to a +/-1 day window, flagged. Never guesses."""
    if schedules is None:
        return None, "no_schedule"
    d = et_date(parse_utc(commence_iso))
    s = schedules[(schedules.home_team == home) & (schedules.away_team == away)]
    days = pd.to_datetime(s.gameday).dt.date
    exact = s[days == d]
    if len(exact) == 1:
        return str(exact.iloc[0].game_id), "team_et_date"
    near = s[days.between(d - timedelta(days=1), d + timedelta(days=1))]
    if len(near) == 1:
        return str(near.iloc[0].game_id), "team_et_date_window"
    return None, "unmatched"


def _nflverse_kickoff(game_id: str | None, schedules: pd.DataFrame | None):
    if game_id is None or schedules is None:
        return None
    row = schedules[schedules.game_id == game_id]
    if len(row) != 1:
        return None
    r = row.iloc[0]
    return nflverse_kickoff_utc(str(r.gameday)[:10], None if pd.isna(r.gametime) else str(r.gametime))


# ---- parsing --------------------------------------------------------------------------------

def _game_context(g: dict, snapshot_dt: datetime, schedules: pd.DataFrame | None) -> dict | None:
    """Identity and kickoff fields shared by totals and spread rows (same rules for both markets)."""
    home, away = to_nflverse_code(g["home_team"]), to_nflverse_code(g["away_team"])
    if home is None or away is None:
        logger.warning("Unmapped team names: %s @ %s", g.get("away_team"), g.get("home_team"))
        return None
    game_id, method = match_game_id(home, away, g["commence_time"], schedules)
    commence = parse_utc(g["commence_time"])
    nfl_k = _nflverse_kickoff(game_id, schedules)
    kr = resolve_game_kickoff(commence, nfl_k)
    kickoff = kr["kickoff"]
    return {"event_id": g["id"], "game_id": game_id, "match_method": method, "home_team": home, "away_team": away,
            "commence_time_utc_as_reported": commence.isoformat(),
            "kickoff_nflverse_utc": nfl_k.isoformat() if nfl_k else None,
            "kickoff_utc_resolved": kickoff.isoformat(), "kickoff_source": kr["source"],
            "kickoff_status": kr["status"], "kickoff_conflict_minutes": kr["conflict_minutes"],
            "kickoff_et": kickoff.astimezone(ET).isoformat(),
            "minutes_to_kickoff": round(minutes_to_kickoff(snapshot_dt, kickoff), 1),
            "is_pregame": is_pregame(snapshot_dt, kickoff)}


SPREAD_FIELDS = ["snapshot_id", "snapshot_ts_utc", "snapshot_kind", "event_id", "game_id", "match_method",
                 "home_team", "away_team", "commence_time_utc_as_reported", "kickoff_nflverse_utc", "kickoff_utc_resolved",
                 "kickoff_source", "kickoff_status", "kickoff_conflict_minutes", "kickoff_et", "minutes_to_kickoff",
                 "is_pregame", "book", "home_spread", "home_price", "away_spread", "away_price",
                 "book_last_update", "quote_age_minutes", "raw_file", "response_sha256", *TIMING_FIELDS]


def parse_spread_quotes(raw_games: list[dict], snapshot_id: str, snapshot_dt: datetime, kind: str,
                        schedules: pd.DataFrame | None, raw_file: str, sha: str, timing: dict | None = None) -> list[dict]:
    """Phase 5A. One row per (game, book) with a valid PAIRED two-way spread: the home team's number and price
    and the away team's number and price from the same book in the same snapshot, with the away number exactly
    the negative of the home number. A book missing either side, naming a team we cannot map, or posting
    numbers that do not mirror is skipped (never patched). Spread sign: home_spread -3.5 = home favored by 3.5."""
    timing = {k: (timing or {}).get(k) for k in TIMING_FIELDS}
    rows = []
    for g in raw_games:
        ctx = _game_context(g, snapshot_dt, schedules)
        if ctx is None:
            continue
        for book in g.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "spreads":
                    continue
                by_team = {}
                for o in market.get("outcomes", []):
                    code = to_nflverse_code(o.get("name"))
                    if code is not None and "point" in o and "price" in o:
                        by_team[code] = o
                h, a = by_team.get(ctx["home_team"]), by_team.get(ctx["away_team"])
                if not (h and a) or float(h["point"]) != -float(a["point"]):
                    continue
                lu = market.get("last_update") or book.get("last_update")
                age = None if not lu else round((snapshot_dt - parse_utc(lu)).total_seconds() / 60.0, 1)
                rows.append({"snapshot_id": snapshot_id, "snapshot_ts_utc": snapshot_dt.isoformat(), "snapshot_kind": kind,
                             **ctx, "book": book["key"],
                             "home_spread": float(h["point"]) + 0.0, "home_price": int(h["price"]),
                             "away_spread": float(a["point"]) + 0.0, "away_price": int(a["price"]),
                             "book_last_update": lu, "quote_age_minutes": age,
                             "raw_file": raw_file, "response_sha256": sha, **timing})
    return rows


def parse_quotes(raw_games: list[dict], snapshot_id: str, snapshot_dt: datetime, kind: str,
                 schedules: pd.DataFrame | None, raw_file: str, sha: str, timing: dict | None = None) -> list[dict]:
    """Pure parsing: one row per (game, book) with a valid paired two-way total. A book whose Over
    and Under points differ, or that lacks either side, is skipped (never patched). `timing` carries the
    snapshot's trigger / scheduled target / delay (blank for snapshots taken before these were recorded)."""
    timing = {k: (timing or {}).get(k) for k in TIMING_FIELDS}
    rows = []
    for g in raw_games:
        home, away = to_nflverse_code(g["home_team"]), to_nflverse_code(g["away_team"])
        if home is None or away is None:
            logger.warning("Unmapped team names: %s @ %s", g.get("away_team"), g.get("home_team"))
            continue
        game_id, method = match_game_id(home, away, g["commence_time"], schedules)
        commence = parse_utc(g["commence_time"])
        nfl_k = _nflverse_kickoff(game_id, schedules)
        kr = resolve_game_kickoff(commence, nfl_k)
        kickoff = kr["kickoff"]
        mtk = round(minutes_to_kickoff(snapshot_dt, kickoff), 1)
        for book in g.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "totals":
                    continue
                over = next((o for o in market.get("outcomes", []) if o.get("name") == "Over"), None)
                under = next((o for o in market.get("outcomes", []) if o.get("name") == "Under"), None)
                if not (over and under and "point" in over and "point" in under):
                    continue
                if float(over["point"]) != float(under["point"]):
                    continue
                lu = market.get("last_update") or book.get("last_update")
                age = None if not lu else round((snapshot_dt - parse_utc(lu)).total_seconds() / 60.0, 1)
                rows.append({
                    "snapshot_id": snapshot_id, "snapshot_ts_utc": snapshot_dt.isoformat(), "snapshot_kind": kind,
                    "event_id": g["id"], "game_id": game_id, "match_method": method,
                    "home_team": home, "away_team": away,
                    "commence_time_utc_as_reported": commence.isoformat(),
                    "kickoff_nflverse_utc": nfl_k.isoformat() if nfl_k else None,
                    "kickoff_utc_resolved": kickoff.isoformat(),
                    "kickoff_source": kr["source"], "kickoff_status": kr["status"],
                    "kickoff_conflict_minutes": kr["conflict_minutes"],
                    "kickoff_et": kickoff.astimezone(ET).isoformat(),
                    "minutes_to_kickoff": mtk, "is_pregame": is_pregame(snapshot_dt, kickoff),
                    "book": book["key"], "total_line": float(over["point"]),
                    "over_price": int(over["price"]), "under_price": int(under["price"]),
                    "book_last_update": lu, "quote_age_minutes": age,
                    "raw_file": raw_file, "response_sha256": sha, **timing,
                })
    return rows


# ---- immutable / atomic writes --------------------------------------------------------------

TMP_PREFIX = ".tmp-"


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:      # directories cannot be opened on Windows; the file itself is already fsynced
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_tmp(directory: Path, name: str, data: bytes) -> Path:
    """Write the COMPLETE payload to a hidden temp file in the destination directory and fsync it."""
    tmp = directory / f"{TMP_PREFIX}{name}-{uuid.uuid4().hex}"
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return tmp


def write_exclusive(path: Path, data: bytes) -> Path:
    """Crash-safe, never-overwriting publication of an immutable record.

    1. write the complete bytes to a hidden temp file (.tmp-...) in the same directory, flush, fsync;
    2. publish with os.link(temp, final): atomic, and it FAILS if the final name exists, so an existing
       record is never overwritten;
    3. on a name collision the data is kept under a -dupN suffix (a paid response is never dropped);
    4. remove the temp name and fsync the directory.
    A crash at any point leaves either no final file or the complete final file, never a partial one.
    A leftover temp file is never read (readers glob only published names), the workflows delete it
    before committing, and the records guard rejects it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_tmp(path.parent, path.name, data)
    try:
        cand, n = path, 0
        while True:
            try:
                os.link(tmp, cand)
                break
            except FileExistsError:
                n += 1
                cand = path.with_name(f"{path.stem}-dup{n}{path.suffix}")
        _fsync_dir(path.parent)
        return cand
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def append_rows_atomic(path: Path, rows: list[dict], fields: list[str]) -> int:
    """Append-only CSV update published atomically: the full new content (old bytes unchanged + new rows)
    is written to a temp file, fsynced, then swapped in with os.replace, so a crash never leaves a torn
    row. Refuses a header mismatch and refuses to extend a file whose final row is already torn."""
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_bytes() if path.exists() else b""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    if old:
        header = old.split(b"\n", 1)[0].decode("utf-8").rstrip("\r")
        if header != ",".join(fields):
            raise RuntimeError(f"{path}: header mismatch; refusing to append")
        if not old.endswith(b"\n"):
            raise RuntimeError(f"{path}: final row is torn (no trailing newline); refusing to append")
    else:
        w.writeheader()
    w.writerows(rows)
    tmp = _write_tmp(path.parent, path.name, old + buf.getvalue().encode("utf-8"))
    try:
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if tmp.exists():
            tmp.unlink()
    return len(rows)


def raw_names(odds_dir: Path = ODDS_DIR) -> list[str]:
    d = odds_dir / "raw"
    return sorted(p.name for p in d.glob("totals_*.json")) if d.exists() else []


def halt_files(odds_dir: Path = ODDS_DIR) -> list[str]:
    d = odds_dir / "raw"
    return sorted(p.name for p in d.glob(f"{HALT_PREFIX}*")) if d.exists() else []


def _csv_bytes(rows: list[dict], fields: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ---- the snapshot ---------------------------------------------------------------------------

def take_snapshot(kind: str, trigger: str = "manual", schedules: pd.DataFrame | None = None,
                  odds_dir: Path = ODDS_DIR, targets: list[dict] | None = None,
                  now: datetime | None = None, preflight: bool = True,
                  scheduled_target: str | None = None, claim_id: str | None = None) -> dict:
    """Budget preflight (free) -> one paid pull -> raw v2 envelope -> paired quotes rows.

    scheduled_target: the ET schedule target this capture serves (scheduled kinds). The capture's own
    timestamp is the real fetch time; the target and the delay are recorded beside it, never instead of it."""
    if trigger not in C.TRIGGERS:
        raise ValueError(f"invalid trigger {trigger}")
    if claim_id:
        # the paid-pull claim this capture fulfils (src/nfl/ops/claims.py); written into the raw envelope so
        # the claim is resolved by THIS capture only, never by an unrelated capture of the same kind
        from src.nfl.ops.claims import CLAIM_ID_RE
        m = CLAIM_ID_RE.match(str(claim_id))
        if not m or m.group(1) != kind:
            raise ValueError(f"claim id {claim_id!r} does not match a {kind} claim")
    if kind not in C.ALL_KINDS or kind == "legacy_current":
        raise ValueError(f"invalid snapshot kind {kind}")
    names = raw_names(odds_dir)
    now = now or datetime.now(UTC)
    if kind == "pregame_window":
        # Re-check immediately before paying: the gate decided minutes ago and this job may have
        # waited in the queue. Uses the records restored into odds_dir, game by game: a game another
        # pull already QUOTED in its window, or whose kickoff is past or inside T-5, is dropped here,
        # and if nothing is left no credit is spent.
        if not targets:
            raise PregameNotNeeded("no target games given")
        events = [(t["event_id"], parse_utc(t["kickoff"])) for t in targets]
        cov = coverage_from_quote_csvs(quote_files(odds_dir))
        d = plan_pregame(now, events, names, coverage=cov)
        if not d.pull:
            raise PregameNotNeeded(d.reason)
    if scheduled_target and parse_utc(scheduled_target) > now:
        # checked BEFORE paying: a capture is never labeled with a target that has not happened yet
        raise ValueError("scheduled target is after the capture time; a capture is never backdated or pre-dated")
    if kind in C.SCHEDULED_KINDS and scheduled_target:
        # Same idea for scheduled kinds: the gate decided before install/data refresh, and a duplicate
        # trigger (external dispatch + GitHub cron twin + manual) may have committed in between. The
        # records restored into odds_dir are re-read here; at most one capture per (kind, target).
        tgt0 = parse_utc(scheduled_target)
        end0 = late_window(kind, tgt0)
        if any(k == kind and tgt0 <= ts < end0 for ts, k in (p for p in (parse_raw_name(n) for n in names) if p)):
            raise ScheduledNotNeeded(f"{kind} already captured for target {tgt0.isoformat()}")
        if now >= end0 and trigger != "manual":
            raise ScheduledNotNeeded(f"{kind} window for target {tgt0.isoformat()} closed at {end0.isoformat()} (obsolete)")
    balance = None
    if preflight:
        halts = halt_files(odds_dir)
        if halts:
            raise BudgetError(f"halted by {halts[0]}; John's approval is needed before any further pull")
        used_week = credits_this_week(now, names, odds_dir)
        if used_week + C.EXPECTED_CREDITS_PER_PULL > C.WEEKLY_CREDIT_CAP:
            raise BudgetError(f"weekly cap reached: {used_week} credits since Tuesday 00:00 ET; another pull costs "
                              f"{C.EXPECTED_CREDITS_PER_PULL} (cap {C.WEEKLY_CREDIT_CAP} credits)")
        balance = fetch_balance()
        if balance["remaining"] is None:
            raise BudgetError("credit balance header missing; refusing to pull (fail closed)")
        # The reserve must still be intact AFTER a normal pull: pay only if remaining - expected >= reserve
        # (reserve 60, 2 credits per totals+spreads pull: 62 or more remaining). A provider charge above the
        # expected credits cannot be prevented by a balance check made before the request; it is detected from
        # the response headers afterwards (HALT file, all further pulls refused).
        after = balance["remaining"] - C.EXPECTED_CREDITS_PER_PULL
        if after < C.CREDIT_RESERVE:
            raise BudgetError(f"credit reserve: {balance['remaining']} remaining would leave {after} after this pull "
                              f"(reserve {C.CREDIT_RESERVE} must remain)")
    res = fetch_totals_raw(request_bookmakers())
    body: bytes = res.pop("body")
    sha = hashlib.sha256(body).hexdigest()
    fetched = parse_utc(res["fetched_at"])
    ts = snapshot_ts(fetched)
    tgt = parse_utc(scheduled_target) if scheduled_target else None
    delay = None if tgt is None else round((fetched - tgt).total_seconds() / 60.0, 1)
    # delay is measured from the API's own fetch time; a few seconds of runner/API clock skew can make it
    # slightly negative, which is recorded as measured (the data is already paid for and is kept)
    timing = {"trigger": trigger, "scheduled_target_utc": tgt.isoformat() if tgt else None,
              "schedule_delay_minutes": delay,
              "delayed": None if delay is None else bool(delay > C.DELAYED_THRESHOLD_MIN)}
    envelope = {"schema_version": RAW_SCHEMA, "snapshot_id": f"{ts}_{kind}", "snapshot_kind": kind, "trigger": trigger,
                "claim_id": claim_id or None, "capture_ts_utc": fetched.isoformat(), **{k: timing[k] for k in TIMING_FIELDS if k != "trigger"},
                **res, "preflight_balance": balance, "spec_version": C.SPEC_VERSION,
                "code_sha": os.environ.get("GITHUB_SHA"), "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
                "targets": targets or [], "response_sha256": sha,
                "response_body": body.decode("utf-8", "replace")}
    raw_path = write_exclusive(odds_dir / "raw" / f"totals_{ts}_{kind}.json",
                               json.dumps(envelope, indent=1).encode("utf-8"))
    # The snapshot id follows the file actually written (a -dupN collision gets its own id).
    snapshot_id = raw_path.stem.removeprefix("totals_")
    if snapshot_id != envelope["snapshot_id"]:
        logger.warning("raw name collision; saved as %s", raw_path.name)
    if res["http_status"] != 200:
        raise RuntimeError(f"Odds API returned {res['http_status']}; raw saved at {raw_path} (no quotes parsed)")
    last = res.get("x_requests_last")
    if last is not None and float(last) > C.EXPECTED_CREDITS_PER_PULL:
        write_exclusive(odds_dir / "raw" / f"{HALT_PREFIX}cost_anomaly_{ts}.json", json.dumps(
            {"snapshot_id": snapshot_id, "x_requests_last": last, "expected": C.EXPECTED_CREDITS_PER_PULL,
             "note": "All further pulls are refused until John approves removing this file."}, indent=1).encode())
    games = json.loads(body)
    rows = parse_quotes(games, snapshot_id, fetched, kind, schedules, raw_path.name, sha, timing)
    if rows:
        write_exclusive(odds_dir / QUOTES_DIR / f"quotes_{snapshot_id}.csv", _csv_bytes(rows, QUOTE_FIELDS))
    srows = parse_spread_quotes(games, snapshot_id, fetched, kind, schedules, raw_path.name, sha, timing)
    if srows:
        write_exclusive(odds_dir / SPREAD_QUOTES_DIR / f"spread_quotes_{snapshot_id}.csv", _csv_bytes(srows, SPREAD_FIELDS))
    summary = {"snapshot_id": snapshot_id, "kind": kind, "raw_path": str(raw_path), "claim_id": claim_id or None,
               "games": len(games), **timing,
               "quote_rows": len(rows), "spread_quote_rows": len(srows),
               "unmatched_games": len({r["event_id"] for r in rows if r["game_id"] is None}),
               "books_returned": sorted({r["book"] for r in rows}), "x_requests_last": last,
               "x_requests_remaining": res.get("x_requests_remaining")}
    logger.info("Odds snapshot %s", json.dumps(summary))
    if last is not None and float(last) > C.EXPECTED_CREDITS_PER_PULL:
        raise CostAnomalyError(f"pull cost {last} credits (expected {C.EXPECTED_CREDITS_PER_PULL}); data saved, stopping")
    summary["games_for_weather"] = sorted({(r["game_id"], r["kickoff_utc_resolved"]) for r in rows
                                           if r["game_id"] and r["is_pregame"]})
    return summary


# ---- readers (derived tables rebuild from raw; raw is the source of truth) ------------------

def iter_raw_snapshots(odds_dir: Path = ODDS_DIR):
    """Yield dicts {snapshot_id, snapshot_dt, kind, raw_file, sha, hash_basis, http_status, games}
    for every raw file, including the single pre-spec legacy file (kind legacy_current)."""
    for name in raw_names(odds_dir):
        parsed = parse_raw_name(name)
        if not parsed:
            continue
        env = json.loads((odds_dir / "raw" / name).read_text(encoding="utf-8"))
        if env.get("schema_version") == RAW_SCHEMA:
            body = env["response_body"].encode("utf-8")
            yield {"snapshot_id": name[len("totals_"):-len(".json")], "snapshot_dt": parse_utc(env["fetched_at"]),
                   "kind": env["snapshot_kind"], "raw_file": name, "sha": env["response_sha256"],
                   "hash_basis": "response_bytes", "hash_ok": hashlib.sha256(body).hexdigest() == env["response_sha256"],
                   "http_status": env["http_status"], "games": json.loads(body) if env["http_status"] == 200 else [],
                   "timing": {k: env.get(k) for k in TIMING_FIELDS}}
        else:  # Phase 2B v1 envelope (one file, Oct 15 2026): original bytes were not kept
            games = env.get("games", [])
            sha = hashlib.sha256(json.dumps(games, sort_keys=True).encode("utf-8")).hexdigest()
            yield {"snapshot_id": f"{parsed[0].strftime('%Y%m%dT%H%M%SZ')}_legacy_current",
                   "snapshot_dt": parse_utc(env["fetched_at"]), "kind": "legacy_current", "raw_file": name,
                   "sha": sha, "hash_basis": "reserialized_games_json", "hash_ok": None, "http_status": 200,
                   "games": games, "timing": {}}


def quotes_from_raw(odds_dir: Path = ODDS_DIR, schedules: pd.DataFrame | None = None) -> pd.DataFrame:
    rows = []
    for snap in iter_raw_snapshots(odds_dir):
        if snap["http_status"] != 200:
            continue
        rows += parse_quotes(snap["games"], snap["snapshot_id"], snap["snapshot_dt"], snap["kind"], schedules,
                             snap["raw_file"], snap["sha"], snap.get("timing"))
    return pd.DataFrame(rows, columns=QUOTE_FIELDS)


def load_quotes(odds_dir: Path = ODDS_DIR) -> pd.DataFrame:
    paths = ([odds_dir / QUOTES_FILE] if (odds_dir / QUOTES_FILE).exists() else []) + \
        sorted((odds_dir / QUOTES_DIR).glob("quotes_*.csv"))
    if not paths:
        return pd.DataFrame(columns=QUOTE_FIELDS)
    return pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)


def spread_quotes_from_raw(odds_dir: Path = ODDS_DIR, schedules: pd.DataFrame | None = None) -> pd.DataFrame:
    """Rebuild paired spread quotes from the raw envelopes (raw is the source of truth). Snapshots taken before
    Phase 5A requested spreads simply contribute no rows."""
    rows = []
    for snap in iter_raw_snapshots(odds_dir):
        if snap["http_status"] != 200:
            continue
        rows += parse_spread_quotes(snap["games"], snap["snapshot_id"], snap["snapshot_dt"], snap["kind"], schedules,
                                    snap["raw_file"], snap["sha"], snap.get("timing"))
    return pd.DataFrame(rows, columns=SPREAD_FIELDS)


def load_spread_quotes(odds_dir: Path = ODDS_DIR) -> pd.DataFrame:
    paths = sorted((odds_dir / SPREAD_QUOTES_DIR).glob("spread_quotes_*.csv"))
    if not paths:
        return pd.DataFrame(columns=SPREAD_FIELDS)
    return pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
