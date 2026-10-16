"""Market fields for the weekly prediction snapshot, built only from paired quotes.

Every price sits next to the exact line it was quoted at:
  market_total            median across ALL observed books (same basis as Phase 2B; used for the
                          ridge8_v1 research lean, which is unchanged); may be a non-bettable .25/.75
  canonical_total         most common posted line, with consensus (median) prices AT that line
  best_over_* / best_under_*        all observed books, each with its own book and price
  ny_best_over_* / ny_best_under_*  NY-ACTIONABLE books only
  ref_*                   Pinnacle (reference only)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.nfl.market.timing import parse_utc


def _f(v):
    return None if v is None or (isinstance(v, float) and v != v) or pd.isna(v) else v


def latest_market_view(ls: pd.DataFrame, game_id: str, max_age_hours: float, now: datetime | None = None) -> dict | None:
    if ls is None or ls.empty:
        return None
    rows = ls[ls.game_id == game_id]
    if rows.empty:
        return None
    rows = rows.assign(_ts=rows.snapshot_ts_utc.map(parse_utc)).sort_values("_ts")
    r = rows.iloc[-1]
    age = ((now or datetime.now(timezone.utc)) - r._ts).total_seconds() / 3600
    if age > max_age_hours or not r.get("all_n_books"):
        return None
    num = lambda k: None if _f(r.get(k)) is None else float(r[k])
    intg = lambda k: None if _f(r.get(k)) is None else int(r[k])
    return {
        "market_total": num("all_median_total"), "market_total_basis": "median_all_observed_books",
        "market_source": f"odds_snapshot:{r.snapshot_id}", "market_snapshot_kind": r.snapshot_kind,
        "n_books": intg("all_n_books"), "lowest_total": num("all_worst_under_total"), "highest_total": num("all_worst_over_total"),
        "canonical_total": num("all_canonical_total"),
        "consensus_over_price_at_canonical": num("all_consensus_over_price_at_canonical"),
        "consensus_under_price_at_canonical": num("all_consensus_under_price_at_canonical"),
        "consensus_over_prob_at_canonical": num("all_consensus_over_prob_at_canonical"),
        "best_over_line": num("all_best_over_total"), "best_over_price": intg("all_best_over_price"), "best_over_book": _f(r.get("all_best_over_book")),
        "best_under_line": num("all_best_under_total"), "best_under_price": intg("all_best_under_price"), "best_under_book": _f(r.get("all_best_under_book")),
        "ny_n_books": intg("ny_n_books"),
        "ny_best_over_line": num("ny_best_over_total"), "ny_best_over_price": intg("ny_best_over_price"), "ny_best_over_book": _f(r.get("ny_best_over_book")),
        "ny_best_under_line": num("ny_best_under_total"), "ny_best_under_price": intg("ny_best_under_price"), "ny_best_under_book": _f(r.get("ny_best_under_book")),
        "ref_book": _f(r.get("ref_book")), "ref_total": num("ref_total"),
        "ref_over_price": intg("ref_over_price"), "ref_under_price": intg("ref_under_price"),
    }
