"""Per-book NFL totals snapshots from The Odds API (Phase 4, spec docs/phase4/PHASE_4_SPEC.md).

One paid call = one snapshot = 1 credit (totals market, the frozen 10-book list in
configs/phase4_books.yaml). Every snapshot is stored two ways, both append-only:

  data/odds/raw/totals_<UTC ts>_<kind>.json   envelope `odds_raw_v2`: request params (never the key),
                                              usage headers, code SHA, workflow run id, SHA-256 of the
                                              exact response bytes, and the response body itself.
                                              Created exclusively; never overwritten.
  data/odds/quotes_v2.csv                     one row per (snapshot, game, book): ONE book, ONE total
                                              line, THAT book's Over and Under prices at that line.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.nfl.data.odds_api import fetch_balance, fetch_totals_raw
from src.nfl.data.team_mapping import to_nflverse_code
from src.nfl.market import phase4_config as C
from src.nfl.market.books import request_bookmakers
from src.nfl.market.timing import (ET, UTC, et_date, is_pregame, minutes_to_kickoff, nflverse_kickoff_utc,
                                   parse_raw_name, parse_utc, pulls_this_week, resolve_kickoff, snapshot_ts)

logger = logging.getLogger(__name__)

ODDS_DIR = Path("data/odds")
RAW_SCHEMA = "odds_raw_v2"
QUOTES_FILE = "quotes_v2.csv"
QUOTE_FIELDS = ["snapshot_id", "snapshot_ts_utc", "snapshot_kind", "event_id", "game_id", "match_method",
                "home_team", "away_team", "commence_time_utc_as_reported", "kickoff_utc_resolved", "kickoff_et",
                "minutes_to_kickoff", "is_pregame", "book", "total_line", "over_price", "under_price",
                "book_last_update", "quote_age_minutes", "raw_file", "response_sha256"]


class BudgetError(RuntimeError):
    """A paid pull was refused before any credit was spent."""


class CostAnomalyError(RuntimeError):
    """The API charged more than the approved plan expects. The raw response is already saved."""


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

def parse_quotes(raw_games: list[dict], snapshot_id: str, snapshot_dt: datetime, kind: str,
                 schedules: pd.DataFrame | None, raw_file: str, sha: str) -> list[dict]:
    """Pure parsing: one row per (game, book) with a valid paired two-way total. A book whose Over
    and Under points differ, or that lacks either side, is skipped (never patched)."""
    rows = []
    for g in raw_games:
        home, away = to_nflverse_code(g["home_team"]), to_nflverse_code(g["away_team"])
        if home is None or away is None:
            logger.warning("Unmapped team names: %s @ %s", g.get("away_team"), g.get("home_team"))
            continue
        game_id, method = match_game_id(home, away, g["commence_time"], schedules)
        commence = parse_utc(g["commence_time"])
        kickoff = resolve_kickoff(commence, _nflverse_kickoff(game_id, schedules))
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
                    "kickoff_utc_resolved": kickoff.isoformat(),
                    "kickoff_et": kickoff.astimezone(ET).isoformat(),
                    "minutes_to_kickoff": mtk, "is_pregame": is_pregame(snapshot_dt, kickoff),
                    "book": book["key"], "total_line": float(over["point"]),
                    "over_price": int(over["price"]), "under_price": int(under["price"]),
                    "book_last_update": lu, "quote_age_minutes": age,
                    "raw_file": raw_file, "response_sha256": sha,
                })
    return rows


# ---- immutable / atomic writes --------------------------------------------------------------

def write_exclusive(path: Path, data: bytes) -> Path:
    """Exclusive create. On a name collision the data is kept under a -dupN suffix (a paid
    response is never dropped) and nothing existing is overwritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cand, n = path, 0
    while True:
        try:
            with open(cand, "xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            return cand
        except FileExistsError:
            n += 1
            cand = path.with_name(f"{path.stem}-dup{n}{path.suffix}")


def append_rows_atomic(path: Path, rows: list[dict], fields: list[str]) -> int:
    """Append-only CSV write as a single buffered write. Refuses a header mismatch and refuses
    to append after a torn (newline-less) final row instead of silently gluing rows together."""
    if not rows:
        return 0
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as f:
            header = f.readline().decode("utf-8").rstrip("\r\n")
            f.seek(-1, os.SEEK_END)
            last = f.read(1)
        if header != ",".join(fields):
            raise RuntimeError(f"{path}: header mismatch; refusing to append")
        if last != b"\n":
            raise RuntimeError(f"{path}: final row is torn (no trailing newline); refusing to append")
    else:
        w.writeheader()
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return len(rows)


def raw_names(odds_dir: Path = ODDS_DIR) -> list[str]:
    d = odds_dir / "raw"
    return sorted(p.name for p in d.glob("totals_*.json")) if d.exists() else []


# ---- the snapshot ---------------------------------------------------------------------------

def take_snapshot(kind: str, trigger: str = "manual", schedules: pd.DataFrame | None = None,
                  odds_dir: Path = ODDS_DIR, target_kickoffs: list[str] | None = None,
                  now: datetime | None = None, preflight: bool = True) -> dict:
    """Budget preflight (free) -> one paid pull -> raw v2 envelope -> paired quotes rows."""
    if kind not in C.ALL_KINDS or kind == "legacy_current":
        raise ValueError(f"invalid snapshot kind {kind}")
    names = raw_names(odds_dir)
    balance = None
    if preflight:
        used_week = pulls_this_week(now or datetime.now(UTC), names)
        if used_week >= C.WEEKLY_CREDIT_CAP:
            raise BudgetError(f"weekly cap reached: {used_week} pulls since Tuesday 00:00 ET (cap {C.WEEKLY_CREDIT_CAP})")
        balance = fetch_balance()
        if balance["remaining"] is not None and balance["remaining"] < C.CREDIT_RESERVE:
            raise BudgetError(f"credit reserve: {balance['remaining']} remaining < reserve {C.CREDIT_RESERVE}")
    res = fetch_totals_raw(request_bookmakers())
    body: bytes = res.pop("body")
    sha = hashlib.sha256(body).hexdigest()
    fetched = parse_utc(res["fetched_at"])
    ts = snapshot_ts(fetched)
    envelope = {"schema_version": RAW_SCHEMA, "snapshot_id": f"{ts}_{kind}", "snapshot_kind": kind, "trigger": trigger,
                **res, "preflight_balance": balance, "spec_version": C.SPEC_VERSION,
                "code_sha": os.environ.get("GITHUB_SHA"), "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
                "target_kickoffs": target_kickoffs or [], "response_sha256": sha,
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
    games = json.loads(body)
    rows = parse_quotes(games, snapshot_id, fetched, kind, schedules, raw_path.name, sha)
    append_rows_atomic(odds_dir / QUOTES_FILE, rows, QUOTE_FIELDS)
    summary = {"snapshot_id": snapshot_id, "kind": kind, "raw_path": str(raw_path), "games": len(games),
               "quote_rows": len(rows), "unmatched_games": len({r["event_id"] for r in rows if r["game_id"] is None}),
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
                   "http_status": env["http_status"], "games": json.loads(body) if env["http_status"] == 200 else []}
        else:  # Phase 2B v1 envelope (one file, Oct 15 2026): original bytes were not kept
            games = env.get("games", [])
            sha = hashlib.sha256(json.dumps(games, sort_keys=True).encode("utf-8")).hexdigest()
            yield {"snapshot_id": f"{parsed[0].strftime('%Y%m%dT%H%M%SZ')}_legacy_current",
                   "snapshot_dt": parse_utc(env["fetched_at"]), "kind": "legacy_current", "raw_file": name,
                   "sha": sha, "hash_basis": "reserialized_games_json", "hash_ok": None, "http_status": 200,
                   "games": games}


def quotes_from_raw(odds_dir: Path = ODDS_DIR, schedules: pd.DataFrame | None = None) -> pd.DataFrame:
    rows = []
    for snap in iter_raw_snapshots(odds_dir):
        if snap["http_status"] != 200:
            continue
        rows += parse_quotes(snap["games"], snap["snapshot_id"], snap["snapshot_dt"], snap["kind"], schedules,
                             snap["raw_file"], snap["sha"])
    return pd.DataFrame(rows, columns=QUOTE_FIELDS)


def load_quotes(odds_dir: Path = ODDS_DIR) -> pd.DataFrame:
    path = odds_dir / QUOTES_FILE
    if not path.exists():
        return pd.DataFrame(columns=QUOTE_FIELDS)
    return pd.read_csv(path)
