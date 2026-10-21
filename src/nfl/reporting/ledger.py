"""Totals results ledger v2 with line/price-correct CLV (Phase 4, spec section 9).

Append-only CSV at data/predictions/totals_ledger_v2.csv (the v1 file was never written). One row
per (prediction snapshot, game, variant) once the game is final. Research rows only: units are
always 0, and `side` is the ridge8_v1 research lean, never a bet.

Terminology: there is no "closing line" or "opening line" here. The reference is the game's
last_observed_pregame snapshot (with its close_quality flag); first_observed_line is the earliest
snapshot we saw. CLV comes from src/nfl/market/clv.py:

  line_clv  OVER: ref_canonical_line - line_taken ; UNDER: line_taken - ref_canonical_line
  price CLV only at an identical total number (same book, or >= 3-book same-line consensus),
  otherwise blank. No combined line+price metric.

Variants for each lean: `consensus_all` (canonical line, median price at that line, all observed
books) and `best_available_ny` (best number and price among NY-actionable books). A lean priced
from nflverse (no odds snapshot) gets one row, variant `nflverse_line`, with CLV blank.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.nfl.data.odds_snapshot import ODDS_DIR, append_rows_atomic, quotes_from_raw
from src.nfl.market.clv import build_clv_events, edge_bucket
from src.nfl.market.phase4_config import FORWARD_SEASON_MIN, SETTLE_MIN_HOURS_AFTER_KICKOFF
from src.nfl.market.timing import nflverse_kickoff_utc
from src.nfl.market.line_shopping import build_line_shopping
from src.nfl.market.movement import apply_game_kickoff, build_movement
from src.nfl.reporting.legacy_ledger import FIELDS as LEGACY_FIELDS
from src.nfl.reporting.legacy_ledger import LEDGER_NAME as LEGACY_LEDGER_NAME
from src.nfl.reporting.legacy_ledger import build_legacy_rows, is_legacy_snapshot

logger = logging.getLogger(__name__)

SCHEMA = "totals_ledger_v2"
PRED_DIR = Path("data/predictions")
LEDGER = PRED_DIR / "totals_ledger_v2.csv"
LEAN_VARIANTS = ("consensus_all", "best_available_ny")
FIELDS = ["game_id", "season", "week", "away_team", "home_team", "snapshot_file", "prediction_ts", "model_version",
          "action_snapshot_id", "action_ts", "minutes_to_kickoff_at_action", "side", "variant",
          "line_taken", "price_taken", "book_taken", "ref_snapshot_id", "ref_ts", "ref_minutes_to_kickoff",
          "ref_close_quality", "ref_canonical_line", "ref_median_line", "line_clv", "line_clv_vs_median",
          "price_clv_same_book", "price_clv_same_line_consensus", "fair_ev_vs_ref", "first_observed_line",
          "projected_total", "model_edge", "edge_bucket", "raw_model_probability", "calibrated_probability",
          "actual_total", "result", "units_risked", "recorded_at", "schema_version"]


def settle(side: str, line: float | None, actual_total: float) -> str:
    if side not in ("OVER", "UNDER") or line is None:
        return "none"
    if actual_total == line:
        return "push"
    if side == "OVER":
        return "win" if actual_total > line else "loss"
    return "win" if actual_total < line else "loss"


def _already(path: Path) -> set[tuple[str, str, str]]:
    if not path.exists():
        return set()
    df = pd.read_csv(path, dtype=str)
    return set(zip(df.snapshot_file, df.game_id, df.variant))


def _pred_snapshots(pred_dir: Path):
    for p in sorted(pred_dir.glob("totals_*.json")):
        if p.name == "totals_index.json":
            continue
        yield p, json.loads(p.read_text())


def build_rows(snap: dict, snap_file: str, game: dict, actual: float, ev: pd.DataFrame, first_line: dict,
               now: str) -> list[dict]:
    side = game.get("lean") if game.get("lean") in ("OVER", "UNDER") else "PASS"
    src = game.get("market_source") or ""
    base = {"game_id": game["game_id"], "season": game["season"], "week": game["week"], "away_team": game["away_team"],
            "home_team": game["home_team"], "snapshot_file": snap_file, "prediction_ts": snap["generated_at"],
            "model_version": snap.get("model_version"), "side": side,
            "first_observed_line": first_line.get(game["game_id"]), "projected_total": game.get("projected_total"),
            "model_edge": game.get("edge"), "edge_bucket": edge_bucket(game.get("edge")),
            "raw_model_probability": game.get("raw_model_probability", game.get("model_over_prob")),
            "calibrated_probability": game.get("calibrated_probability"), "actual_total": actual,
            "units_risked": 0.0, "recorded_at": now, "schema_version": SCHEMA}
    if side == "PASS" or not src:
        return [{**base, "variant": "none", "result": "none"}]
    if not src.startswith("odds_snapshot:"):
        lt = game.get("market_total")
        price = game.get("over_price") if side == "OVER" else game.get("under_price")
        return [{**base, "variant": "nflverse_line", "line_taken": lt, "price_taken": price,
                 "result": settle(side, lt, actual)}]
    sid = src.split(":", 1)[1]
    out = []
    for variant in LEAN_VARIANTS:
        m = ev[(ev.action_snapshot_id == sid) & (ev.game_id == game["game_id"]) & (ev.side == side) & (ev.variant == variant)]
        if m.empty:  # S was itself the last pregame snapshot, or no NY book quoted at S: nothing to compare
            out.append({**base, "variant": variant, "action_snapshot_id": sid, "result": "none"})
            continue
        e = m.iloc[0]
        out.append({**base, "variant": variant, "action_snapshot_id": sid, "action_ts": e.action_ts,
                    "minutes_to_kickoff_at_action": e.action_minutes_to_kickoff, "line_taken": e.line_taken,
                    "price_taken": e.price_taken, "book_taken": e.book_taken, "ref_snapshot_id": e.ref_snapshot_id,
                    "ref_ts": e.ref_ts, "ref_minutes_to_kickoff": e.ref_minutes_to_kickoff,
                    "ref_close_quality": e.ref_close_quality, "ref_canonical_line": e.ref_canonical_line,
                    "ref_median_line": e.ref_median_line, "line_clv": e.line_clv, "line_clv_vs_median": e.line_clv_vs_median,
                    "price_clv_same_book": e.price_clv_same_book,
                    "price_clv_same_line_consensus": e.price_clv_same_line_consensus,
                    "fair_ev_vs_ref": e.fair_ev_vs_ref, "result": settle(side, float(e.line_taken), actual)})
    return out


def is_verified_final(row, now: datetime) -> bool:
    """A final score is used only when nflverse reports both scores AND the game's scheduled kickoff
    (US Eastern, zoneinfo) is at least SETTLE_MIN_HOURS_AFTER_KICKOFF in the past."""
    if pd.isna(row.get("home_score")) or pd.isna(row.get("away_score")):
        return False
    k = settle_reference_kickoff(row)
    return k is not None and now >= k + timedelta(hours=SETTLE_MIN_HOURS_AFTER_KICKOFF)


def settle_reference_kickoff(row) -> datetime | None:
    """Scheduled kickoff (US Eastern wall clock). Without a gametime, the END of the ET game day is used,
    so a missing time can only delay settlement, never make it early."""
    gameday = row.get("gameday")
    if gameday is None or pd.isna(gameday):
        return None
    gt = row.get("gametime")
    k = nflverse_kickoff_utc(str(gameday)[:10], None if gt is None or pd.isna(gt) else str(gt))
    return k if k is not None else nflverse_kickoff_utc(str(gameday)[:10], "23:59")


def record_results(schedules: pd.DataFrame, pred_dir: Path = PRED_DIR, ledger: Path = LEDGER,
                   odds_dir: Path = ODDS_DIR, legacy_ledger: Path | None = None,
                   now: datetime | None = None) -> int:
    """Append settled rows for every totals prediction snapshot whose games are final.

    Phase 4 snapshots go to totals_ledger_v2.csv (schema unchanged). Pre-spec snapshots (the Oct 15 2026
    Week 6 file) go to the separate totals_ledger_legacy_pre_spec.csv as variant `legacy_pre_spec`
    (src/nfl/reporting/legacy_ledger.py); they never produce v2 executable-quote rows.
    Returns the total number of rows appended across both files."""
    legacy_ledger = legacy_ledger or ledger.with_name(LEGACY_LEDGER_NAME)
    now = now or datetime.now(timezone.utc)
    schedules = schedules[schedules.season >= FORWARD_SEASON_MIN]   # Phase 4 is 2026+ only (holdout sealed)
    by_id = schedules.set_index("game_id")
    done = _already(ledger)
    done_legacy = _already(legacy_ledger)
    quotes = apply_game_kickoff(quotes_from_raw(odds_dir, schedules), schedules)
    ev = build_clv_events(quotes)
    ls = build_line_shopping(quotes) if not quotes.empty else pd.DataFrame()
    mv = build_movement(ls, quotes) if not ls.empty else pd.DataFrame()
    first_line = dict(zip(mv.game_id, mv.first_observed_canonical)) if not mv.empty else {}
    stamp = datetime.now(timezone.utc).isoformat()
    new, new_legacy = [], []
    for path, snap in _pred_snapshots(pred_dir):
        legacy = is_legacy_snapshot(snap)
        for g in snap["games"]:
            gid = g["game_id"]
            if gid not in by_id.index:
                continue
            row = by_id.loc[gid]
            if not is_verified_final(row, now):
                continue  # not final yet (or too soon after kickoff); a later settlement run picks it up
            actual = float(row["home_score"] + row["away_score"])
            if legacy:
                for r in build_legacy_rows(snap, path.name, g, actual, quotes, odds_dir, stamp):
                    key = (path.name, gid, r["variant"])
                    if key not in done_legacy:
                        new_legacy.append(r)
                        done_legacy.add(key)
                continue
            for r in build_rows(snap, path.name, g, actual, ev, first_line, stamp):
                key = (path.name, gid, r["variant"])
                if key in done:
                    continue
                new.append(r)
                done.add(key)
    if new_legacy:
        append_rows_atomic(legacy_ledger, new_legacy, LEGACY_FIELDS)
        logger.info("Appended %d legacy_pre_spec rows to %s", len(new_legacy), legacy_ledger)
    if new:
        append_rows_atomic(ledger, new, FIELDS)
        logger.info("Appended %d rows to %s", len(new), ledger)
    if not new and not new_legacy:
        logger.info("No new completed games to record.")
    return len(new) + len(new_legacy)


def summarize(ledger: Path = LEDGER) -> dict:
    if not ledger.exists():
        return {"rows": 0, "schema_version": SCHEMA}
    df = pd.read_csv(ledger)
    leans = df[df.side.isin(["OVER", "UNDER"])]
    out = {"rows": int(len(df)), "schema_version": SCHEMA, "units_risked": 0.0}
    for v, d in leans.groupby("variant"):
        dec = d[d.result.isin(["win", "loss"])]
        c = d.line_clv.dropna()
        out[v] = {"leans": int(len(d)), "wins": int((dec.result == "win").sum()), "losses": int((dec.result == "loss").sum()),
                  "mean_line_clv": round(float(c.mean()), 3) if len(c) else None,
                  "median_line_clv": float(c.median()) if len(c) else None}
    return out
