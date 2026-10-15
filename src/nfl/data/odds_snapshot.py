"""Per-book NFL totals snapshots from The Odds API (Phase 2B).

One call = one snapshot. Every snapshot is stored three ways, all append-only:

  data/odds/raw/totals_<UTC ts>_<tag>.json     the untouched API payload (immutable)
  data/odds/book_lines.csv                     one row per (snapshot, game, book):
                                               total_line, over_price, under_price
  data/odds/game_summary.csv                   one row per (snapshot, game):
                                               market_median, lowest_total, highest_total,
                                               best Over number/price/book, best Under
                                               number/price/book, de-vigged consensus
                                               P(over), n_books, timestamp, tag

Tags: "opener" (the Tuesday snapshot that starts the week's line-movement record),
"current" (any mid-week pull), "pregame" (a pull taken as close to kickoff as the
schedule allows). CLV in the results ledger uses the last pregame-or-current
snapshot before kickoff as the closing line, falling back to nflverse's closing line.

Cost: one request per snapshot (one sport, one market, one region = 1 credit).
Fourteen pulls a week is about 15 credits; the free 500-credit tier is plenty.

"Best number" convention: an Over bettor wants the LOWEST total (then the best price);
an Under bettor wants the HIGHEST total (then the best price). Ties on the number are
broken by price (higher American price is better for the bettor).
"""
from __future__ import annotations

import csv
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.nfl.data.odds_api import fetch_game_odds
from src.nfl.data.team_mapping import to_nflverse_code
from src.nfl.models.moneyline.baselines import american_to_prob

logger = logging.getLogger(__name__)

ODDS_DIR = Path("data/odds")
BOOK_FIELDS = ["snapshot_ts", "tag", "game_id", "event_id", "commence_time", "home_team", "away_team",
               "book", "total_line", "over_price", "under_price", "book_last_update"]
SUMMARY_FIELDS = ["snapshot_ts", "tag", "game_id", "event_id", "commence_time", "home_team", "away_team",
                  "n_books", "market_median", "lowest_total", "highest_total",
                  "best_over_line", "best_over_price", "best_over_book",
                  "best_under_line", "best_under_price", "best_under_book",
                  "consensus_over_prob", "consensus_under_prob", "matched_schedule"]


def _price_better(a: float, b: float) -> bool:
    """American odds: is price a better for the bettor than b?"""
    return a > b


def _devig(p_over_raw: float, p_under_raw: float) -> float:
    return p_over_raw / (p_over_raw + p_under_raw)


def match_game_id(home: str, away: str, commence_iso: str, schedules: pd.DataFrame | None) -> str | None:
    """Match an Odds API event to a nflverse game_id by team codes and a +/-1 day
    window around the kickoff date in US Eastern time. Returns None when unmatched
    (never guessed)."""
    if schedules is None:
        return None
    kickoff = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
    # ET is UTC-4 or UTC-5; a 1-day window on either side absorbs the difference.
    day = (kickoff - timedelta(hours=5)).date()
    s = schedules[(schedules.home_team == home) & (schedules.away_team == away)]
    s = s[pd.to_datetime(s.gameday).dt.date.between(day - timedelta(days=1), day + timedelta(days=1))]
    if len(s) == 1:
        return str(s.iloc[0].game_id)
    return None


def parse_totals_payload(raw_games: list[dict], snapshot_ts: str, tag: str,
                         schedules: pd.DataFrame | None = None) -> tuple[list[dict], list[dict]]:
    """Pure parsing, no I/O: returns (book_rows, summary_rows)."""
    book_rows, summary_rows = [], []
    for g in raw_games:
        home = to_nflverse_code(g["home_team"]); away = to_nflverse_code(g["away_team"])
        if home is None or away is None:
            logger.warning("Unmapped team names: %s @ %s", g["away_team"], g["home_team"])
            continue
        game_id = match_game_id(home, away, g["commence_time"], schedules)
        per_book = []
        for book in g.get("bookmakers", []):
            for market in book.get("markets", []):
                if market["key"] != "totals":
                    continue
                over = next((o for o in market["outcomes"] if o["name"] == "Over"), None)
                under = next((o for o in market["outcomes"] if o["name"] == "Under"), None)
                if not (over and under and "point" in over and "point" in under):
                    continue
                if over["point"] != under["point"]:
                    continue  # malformed two-way; skip, never patch
                row = {"snapshot_ts": snapshot_ts, "tag": tag, "game_id": game_id, "event_id": g["id"],
                       "commence_time": g["commence_time"], "home_team": home, "away_team": away,
                       "book": book["key"], "total_line": float(over["point"]),
                       "over_price": int(over["price"]), "under_price": int(under["price"]),
                       "book_last_update": market.get("last_update")}
                per_book.append(row); book_rows.append(row)
        if not per_book:
            continue
        lines = [r["total_line"] for r in per_book]
        best_over = min(per_book, key=lambda r: (r["total_line"], -r["over_price"]))
        best_under = max(per_book, key=lambda r: (r["total_line"], r["under_price"]))
        probs = [_devig(float(american_to_prob(pd.Series([r["over_price"]])).iloc[0]),
                        float(american_to_prob(pd.Series([r["under_price"]])).iloc[0])) for r in per_book]
        p_over = sum(probs) / len(probs)
        summary_rows.append({
            "snapshot_ts": snapshot_ts, "tag": tag, "game_id": game_id, "event_id": g["id"],
            "commence_time": g["commence_time"], "home_team": home, "away_team": away,
            "n_books": len(per_book), "market_median": statistics.median(lines),
            "lowest_total": min(lines), "highest_total": max(lines),
            "best_over_line": best_over["total_line"], "best_over_price": best_over["over_price"], "best_over_book": best_over["book"],
            "best_under_line": best_under["total_line"], "best_under_price": best_under["under_price"], "best_under_book": best_under["book"],
            "consensus_over_prob": round(p_over, 4), "consensus_under_prob": round(1 - p_over, 4),
            "matched_schedule": game_id is not None,
        })
    return book_rows, summary_rows


def _append_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    write_header = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        w.writerows(rows)


def take_snapshot(tag: str = "current", odds_dir: Path = ODDS_DIR, schedules: pd.DataFrame | None = None,
                  regions: str = "us") -> dict:
    """Fetch the totals market from every US book, store raw + parsed, return a summary."""
    raw, meta = fetch_game_odds(markets="totals", regions=regions)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (odds_dir / "raw").mkdir(parents=True, exist_ok=True)
    raw_path = odds_dir / "raw" / f"totals_{ts}_{tag}.json"
    raw_path.write_text(json.dumps({"fetched_at": meta["fetched_at"], "tag": tag, "requests_remaining": meta["requests_remaining"],
                                    "markets": "totals", "regions": regions, "games": raw}, indent=1))
    book_rows, summary_rows = parse_totals_payload(raw, ts, tag, schedules)
    _append_csv(odds_dir / "book_lines.csv", book_rows, BOOK_FIELDS)
    _append_csv(odds_dir / "game_summary.csv", summary_rows, SUMMARY_FIELDS)
    unmatched = sum(1 for r in summary_rows if not r["matched_schedule"])
    logger.info("Odds snapshot %s tag=%s games=%d book_rows=%d unmatched=%d requests_remaining=%s",
                ts, tag, len(summary_rows), len(book_rows), unmatched, meta["requests_remaining"])
    return {"snapshot_ts": ts, "tag": tag, "raw_path": str(raw_path), "games": len(summary_rows),
            "book_rows": len(book_rows), "unmatched": unmatched, "requests_remaining": meta["requests_remaining"]}


def load_game_summary(odds_dir: Path = ODDS_DIR) -> pd.DataFrame:
    path = odds_dir / "game_summary.csv"
    if not path.exists():
        return pd.DataFrame(columns=SUMMARY_FIELDS)
    df = pd.read_csv(path)
    df["snapshot_dt"] = pd.to_datetime(df.snapshot_ts, format="%Y%m%dT%H%M%SZ", utc=True)
    df["commence_dt"] = pd.to_datetime(df.commence_time, utc=True)
    return df


def latest_pregame_summary(game_id: str, odds_dir: Path = ODDS_DIR, summary: pd.DataFrame | None = None) -> dict | None:
    """Last snapshot row for a game taken before its kickoff (the 'closing' snapshot)."""
    s = summary if summary is not None else load_game_summary(odds_dir)
    if s.empty:
        return None
    rows = s[(s.game_id == game_id) & (s.snapshot_dt < s.commence_dt)].sort_values("snapshot_dt")
    return rows.iloc[-1].to_dict() if len(rows) else None


def opener_summary(game_id: str, odds_dir: Path = ODDS_DIR, summary: pd.DataFrame | None = None) -> dict | None:
    s = summary if summary is not None else load_game_summary(odds_dir)
    if s.empty:
        return None
    rows = s[(s.game_id == game_id) & (s.tag == "opener")].sort_values("snapshot_dt")
    return rows.iloc[0].to_dict() if len(rows) else None
