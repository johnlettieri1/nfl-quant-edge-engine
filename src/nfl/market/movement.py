"""Phase 4 per-game movement records (spec section 4, table D2).

first_observed = earliest pregame snapshot quoting the game (never called an opener).
last_observed_pregame = latest snapshot strictly before the game's resolved kickoff (never called
a close). close_quality = taken 0-30 min before kickoff with >= 5 books (all observed scope).
"""
from __future__ import annotations

import json

import pandas as pd

from src.nfl.market.line_shopping import game_key
from src.nfl.market.timing import close_quality, minutes_to_kickoff, parse_utc


def apply_game_kickoff(quotes: pd.DataFrame) -> pd.DataFrame:
    """One kickoff per game: the resolved kickoff carried by the game's LATEST snapshot (which is
    already min(latest Odds API commence_time, nflverse kickoff)). Pregame flags and minutes are then
    recomputed against that single kickoff, so a moved game can never let a post-kickoff snapshot
    count as pregame."""
    if quotes.empty:
        return quotes.assign(game_key=[], kickoff_game=[], is_pregame_game=[], minutes_to_kickoff_game=[])
    q = quotes.copy()
    q["game_key"] = q.apply(game_key, axis=1)
    q["_ts"] = q.snapshot_ts_utc.map(parse_utc)
    latest = q.sort_values("_ts").groupby("game_key").tail(1).set_index("game_key").kickoff_utc_resolved
    k = latest.map(parse_utc)
    q["kickoff_game"] = q.game_key.map(k)
    q["is_pregame_game"] = q._ts < q.kickoff_game
    q["minutes_to_kickoff_game"] = [round(minutes_to_kickoff(a, b), 1) for a, b in zip(q._ts, q.kickoff_game)]
    q["kickoff_game"] = q.kickoff_game.map(lambda d: d.isoformat())
    return q.drop(columns="_ts")


def pregame_snapshots(ls: pd.DataFrame, gk: str) -> pd.DataFrame:
    g = ls[ls.game_key == gk].copy()
    g["_ts"] = g.snapshot_ts_utc.map(parse_utc)
    return g.sort_values("_ts")


def build_movement(ls: pd.DataFrame, quotes: pd.DataFrame, finals: dict | None = None) -> pd.DataFrame:
    """ls = line-shopping table (pregame rows only); quotes = game-kickoff-applied quotes;
    finals = {game_id: actual_total} for games already final (else blank)."""
    finals = finals or {}
    rows = []
    for gk in sorted(ls.game_key.unique()) if len(ls) else []:
        g = pregame_snapshots(ls, gk)
        first, last = g.iloc[0], g.iloc[-1]
        fq = quotes[(quotes.snapshot_id == first.snapshot_id) & (quotes.game_key == gk)]
        can = list(g.all_canonical_total)
        changes = sum(1 for a, b in zip(can, can[1:]) if a != b)
        move = round(float(last.all_canonical_total - first.all_canonical_total), 2)
        days = max((last._ts - first._ts).total_seconds() / 86400.0, 0.0)
        imax, imin = g.all_canonical_total.idxmax(), g.all_canonical_total.idxmin()
        mtk_last = float(last.minutes_to_kickoff)
        rows.append({
            "game_key": gk, "game_id": first.game_id, "home_team": first.home_team, "away_team": first.away_team,
            "kickoff_utc": first.kickoff_utc_resolved,
            "first_observed_snapshot_id": first.snapshot_id, "first_observed_ts": first.snapshot_ts_utc,
            "first_observed_kind": first.snapshot_kind, "first_observed_minutes_to_kickoff": float(first.minutes_to_kickoff),
            "first_observed_median": first.all_median_total, "first_observed_canonical": first.all_canonical_total,
            "first_observed_books": json.dumps([{"book": r.book, "line": r.total_line, "over": int(r.over_price),
                                                 "under": int(r.under_price)} for r in fq.sort_values("book").itertuples()]),
            "n_snapshots": int(len(g)), "intermediate_snapshot_ids": ";".join(g.snapshot_id.iloc[1:-1]),
            "last_observed_pregame_snapshot_id": last.snapshot_id, "last_observed_pregame_ts": last.snapshot_ts_utc,
            "last_observed_pregame_kind": last.snapshot_kind, "last_observed_pregame_minutes_to_kickoff": mtk_last,
            "last_observed_pregame_median": last.all_median_total, "last_observed_pregame_canonical": last.all_canonical_total,
            "last_observed_pregame_n_books": int(last.all_n_books),
            "close_quality": close_quality(mtk_last, int(last.all_n_books)),
            "move_direction": "up" if move > 0 else "down" if move < 0 else "flat", "move_abs_pts": abs(move),
            "move_signed_pts": move,
            "max_total": float(g.all_canonical_total.max()), "max_total_ts": g.loc[imax, "snapshot_ts_utc"],
            "min_total": float(g.all_canonical_total.min()), "min_total_ts": g.loc[imin, "snapshot_ts_utc"],
            "n_line_changes": changes, "velocity_pts_per_day": round(move / days, 3) if days > 0 else None,
            "dispersion_first": first.all_line_range, "dispersion_last": last.all_line_range,
            "best_over_first": first.all_best_over_total, "best_over_last": last.all_best_over_total,
            "best_over_change": round(float(last.all_best_over_total - first.all_best_over_total), 2),
            "best_under_first": first.all_best_under_total, "best_under_last": last.all_best_under_total,
            "best_under_change": round(float(last.all_best_under_total - first.all_best_under_total), 2),
            "ny_best_over_first": first.get("ny_best_over_total"), "ny_best_over_last": last.get("ny_best_over_total"),
            "ny_best_under_first": first.get("ny_best_under_total"), "ny_best_under_last": last.get("ny_best_under_total"),
            "actual_total": finals.get(first.game_id),
        })
    return pd.DataFrame(rows)
