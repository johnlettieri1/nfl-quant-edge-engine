"""Totals results ledger with closing-line value (Phase 2B).

Append-only CSV at data/predictions/totals_ledger.csv. One row per (prediction
snapshot, game) once the game has a final score. Never edits a snapshot.

Columns (in this order):
  game_id, season, week, away_team, home_team, snapshot_file,
  prediction_ts        when the snapshot was generated (UTC)
  model_version        e.g. ridge8_v1 (from the snapshot)
  projected_total
  bet_side             OVER / UNDER / PASS (model lean until the decision layer exists)
  line_taken           total line in the snapshot at prediction time
  price_taken          American price for bet_side at prediction time (if known)
  line_source          where line_taken came from (odds_snapshot:<ts> or nflverse)
  closing_line         last pre-kickoff odds snapshot median, else nflverse closing total
  closing_price        price for bet_side at close (same source rule)
  closing_source
  opening_line         Tuesday opener snapshot median, if one exists
  actual_total
  result               win / loss / push / none
  clv_points           favorable line movement for bet_side: OVER -> closing - taken,
                       UNDER -> taken - closing. Positive = you beat the close.
  clv_price_prob       implied-probability gain on price: implied(closing) - implied(taken)
                       for the same side (positive = your price was better). Blank if a
                       price is missing.
  units_risked         from the snapshot (0 until the decision layer is built)
  units_won_lost       at the price taken (0 if units_risked is 0)
  recorded_at
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.nfl.data.odds_snapshot import load_game_summary, latest_pregame_summary, opener_summary

logger = logging.getLogger(__name__)

PRED_DIR = Path("data/predictions")
LEDGER = PRED_DIR / "totals_ledger.csv"
FIELDS = ["game_id", "season", "week", "away_team", "home_team", "snapshot_file", "prediction_ts", "model_version",
          "projected_total", "bet_side", "line_taken", "price_taken", "line_source",
          "closing_line", "closing_price", "closing_source", "opening_line",
          "actual_total", "result", "clv_points", "clv_price_prob", "units_risked", "units_won_lost", "recorded_at"]


def implied_prob(american: float | None) -> float | None:
    if american is None or pd.isna(american):
        return None
    a = float(american)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def settle(side: str, line: float, actual_total: float) -> str:
    if side not in ("OVER", "UNDER"):
        return "none"
    if actual_total == line:
        return "push"
    if side == "OVER":
        return "win" if actual_total > line else "loss"
    return "win" if actual_total < line else "loss"


def clv_points(side: str, line_taken: float, closing_line: float | None) -> float | None:
    if closing_line is None or pd.isna(closing_line) or side not in ("OVER", "UNDER"):
        return None
    return round(float(closing_line - line_taken) if side == "OVER" else float(line_taken - closing_line), 2)


def clv_price(price_taken: float | None, closing_price: float | None) -> float | None:
    a, b = implied_prob(price_taken), implied_prob(closing_price)
    if a is None or b is None:
        return None
    return round(b - a, 4)


def pnl(result: str, price_taken: float | None, units: float) -> float:
    if units == 0 or result in ("push", "none"):
        return 0.0
    p = implied_prob(price_taken) if price_taken is not None else implied_prob(-110)
    b = (1 - p) / p  # decimal profit per unit
    return round(units * b, 4) if result == "win" else round(-units, 4)


def build_row(snapshot: dict, snapshot_file: str, game: dict, sched_row: pd.Series, odds_summary: pd.DataFrame | None) -> dict | None:
    if pd.isna(sched_row["home_score"]) or pd.isna(sched_row["away_score"]):
        return None
    actual = float(sched_row["home_score"] + sched_row["away_score"])
    side = game.get("lean") or "PASS"
    line_taken = game.get("market_total")
    if line_taken is None:
        return None
    price_taken = game.get("over_price") if side == "OVER" else game.get("under_price") if side == "UNDER" else None
    gid = game["game_id"]
    pre = latest_pregame_summary(gid, summary=odds_summary) if odds_summary is not None else None
    if pre is not None:
        closing_line = float(pre["market_median"]); closing_source = f"odds_snapshot:{pre['snapshot_ts']}"
        closing_price = pre["best_over_price"] if side == "OVER" else pre["best_under_price"] if side == "UNDER" else None
    else:
        closing_line = float(sched_row["total_line"]) if pd.notna(sched_row.get("total_line")) else None
        closing_source = "nflverse_closing"
        closing_price = sched_row.get("over_odds") if side == "OVER" else sched_row.get("under_odds") if side == "UNDER" else None
        closing_price = None if closing_price is None or pd.isna(closing_price) else float(closing_price)
    op = opener_summary(gid, summary=odds_summary) if odds_summary is not None else None
    result = settle(side, float(line_taken), actual)
    units = float(game.get("recommended_units") or 0.0)
    return {"game_id": gid, "season": game["season"], "week": game["week"], "away_team": game["away_team"], "home_team": game["home_team"],
            "snapshot_file": snapshot_file, "prediction_ts": snapshot["generated_at"], "model_version": snapshot.get("model_version"),
            "projected_total": game.get("projected_total"), "bet_side": side, "line_taken": line_taken, "price_taken": price_taken,
            "line_source": game.get("market_source"), "closing_line": closing_line, "closing_price": closing_price,
            "closing_source": closing_source, "opening_line": (float(op["market_median"]) if op is not None else None),
            "actual_total": actual, "result": result, "clv_points": clv_points(side, float(line_taken), closing_line),
            "clv_price_prob": clv_price(price_taken, closing_price), "units_risked": units,
            "units_won_lost": pnl(result, price_taken, units), "recorded_at": datetime.now(timezone.utc).isoformat()}


def _already(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    df = pd.read_csv(path)
    return set(zip(df.snapshot_file.astype(str), df.game_id.astype(str)))


def record_results(schedules: pd.DataFrame, pred_dir: Path = PRED_DIR, ledger: Path = LEDGER) -> int:
    """Append settled rows for every totals snapshot whose games have finished."""
    by_id = schedules.set_index("game_id")
    done = _already(ledger)
    odds_summary = load_game_summary()
    if odds_summary.empty:
        odds_summary = None
    new = []
    for snap_path in sorted(pred_dir.glob("totals_*.json")):
        if snap_path.name == "totals_index.json":
            continue
        snap = json.loads(snap_path.read_text())
        for g in snap["games"]:
            key = (snap_path.name, g["game_id"])
            if key in done or g["game_id"] not in by_id.index:
                continue
            row = build_row(snap, snap_path.name, g, by_id.loc[g["game_id"]], odds_summary)
            if row:
                new.append(row); done.add(key)
    if not new:
        logger.info("No new completed games to record.")
        return 0
    header = not ledger.exists()
    with open(ledger, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if header:
            w.writeheader()
        w.writerows(new)
    logger.info("Appended %d rows to %s", len(new), ledger)
    return len(new)


def summarize(ledger: Path = LEDGER) -> dict:
    if not ledger.exists():
        return {"rows": 0}
    df = pd.read_csv(ledger)
    bet = df[df.bet_side.isin(["OVER", "UNDER"])]
    dec = bet[bet.result.isin(["win", "loss"])]
    return {"rows": int(len(df)), "leans": int(len(bet)), "wins": int((dec.result == "win").sum()), "losses": int((dec.result == "loss").sum()),
            "pushes": int((bet.result == "push").sum()), "win_pct": (float((dec.result == "win").mean()) if len(dec) else None),
            "mean_clv_points": (float(bet.clv_points.mean()) if bet.clv_points.notna().any() else None),
            "units_risked": float(bet.units_risked.sum()), "units_won_lost": float(bet.units_won_lost.sum())}
