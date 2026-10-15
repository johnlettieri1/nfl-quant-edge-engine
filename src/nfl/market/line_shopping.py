"""Phase 4 line-shopping table (spec section 10, Amendment A1 book scopes).

One row per (snapshot, game). Each metric is computed separately for the NY-ACTIONABLE scope
(prefix `ny_`) and the ALL-OBSERVED scope (prefix `all_`), plus the reference/sharp book
(prefix `ref_`, Pinnacle) where it quoted the game. Line value and price value are separate:
prices are only ever compared at the SAME total number; when that is impossible the price
fields are blank.
"""
from __future__ import annotations

import statistics

import pandas as pd

from src.nfl.market import phase4_config as C
from src.nfl.market.books import NY_ACTIONABLE, REFERENCE_SHARP, group_of
from src.nfl.market.odds_math import canonical_line, cents, devig_over, implied_prob, median_price


def game_key(row) -> str:
    gid = row["game_id"]
    return str(gid) if isinstance(gid, str) and gid else f"event:{row['event_id']}"


def _best(q: pd.DataFrame, side: str) -> dict:
    """OVER: lowest line, then best Over price at that line. UNDER: highest line, then best Under
    price at that line. Ties on price -> alphabetical book (deterministic). Price stays paired."""
    price_col = "over_price" if side == "OVER" else "under_price"
    line = q.total_line.min() if side == "OVER" else q.total_line.max()
    at = q[q.total_line == line].copy()
    at["_c"] = at[price_col].map(cents)
    top = at[at._c == at._c.max()].sort_values("book")
    r = top.iloc[0]
    return {"line": float(line), "price": int(r[price_col]), "book": r["book"],
            "books_at_line": ",".join(sorted(at.book)), "books_at_best": ",".join(top.book)}


def shop_metrics(q: pd.DataFrame) -> dict:
    """Metrics for one snapshot x game x scope (q = valid paired quotes in scope)."""
    n = int(len(q))
    if n == 0:
        return {"n_books": 0}
    lines = [float(x) for x in q.total_line]
    med, can = statistics.median(lines), canonical_line(lines)
    bo, bu = _best(q, "OVER"), _best(q, "UNDER")
    out = {"n_books": n, "median_total": med, "canonical_total": can,
           "best_over_total": bo["line"], "best_over_price": bo["price"], "best_over_book": bo["book"],
           "best_over_books_at_line": bo["books_at_line"],
           "best_under_total": bu["line"], "best_under_price": bu["price"], "best_under_book": bu["book"],
           "best_under_books_at_line": bu["books_at_line"],
           "worst_over_total": max(lines), "worst_under_total": min(lines),
           "over_line_adv_pts": round(can - bo["line"], 2), "under_line_adv_pts": round(bu["line"] - can, 2),
           "over_line_adv_vs_median": round(med - bo["line"], 2), "under_line_adv_vs_median": round(bu["line"] - med, 2),
           "line_range": round(max(lines) - min(lines), 2),
           "line_std": round(statistics.pstdev(lines), 3), "n_distinct_lines": len(set(lines)),
           "over_crosses_key": any(bo["line"] < k <= can for k in C.KEY_TOTALS),
           "under_crosses_key": any(can <= k < bu["line"] for k in C.KEY_TOTALS)}
    at = q[q.total_line == can]
    out["n_books_at_canonical"] = int(len(at))
    out["consensus_over_prob_at_canonical"] = round(statistics.fmean(
        devig_over(o, u) for o, u in zip(at.over_price, at.under_price)), 4) if len(at) else None
    for side, col in (("over", "over_price"), ("under", "under_price")):
        if len(at) >= 2:
            cons = median_price(list(at[col]))
            best = max(at[col], key=cents)
            out[f"consensus_{side}_price_at_canonical"] = cons
            out[f"best_{side}_price_at_canonical"] = int(best)
            out[f"price_adv_cents_{side}"] = round(cents(best) - cents(cons), 1)
            out[f"price_adv_prob_{side}"] = round(implied_prob(cons) - implied_prob(best), 4)
        else:  # fewer than 2 books at the same number: no same-line price comparison is possible
            out[f"consensus_{side}_price_at_canonical"] = int(at[col].iloc[0]) if len(at) == 1 else None
            out[f"best_{side}_price_at_canonical"] = None
            out[f"price_adv_cents_{side}"] = None
            out[f"price_adv_prob_{side}"] = None
    if "quote_age_minutes" in q:
        stale = q[pd.to_numeric(q.quote_age_minutes, errors="coerce") > C.STALE_QUOTE_MIN].book
        out["stale_books"] = ",".join(sorted(stale))
    return out


def reference_metrics(q: pd.DataFrame) -> dict:
    ref = q[q.book.map(group_of) == REFERENCE_SHARP].sort_values("book")
    if ref.empty:
        return {"ref_book": None}
    r = ref.iloc[0]
    return {"ref_book": r.book, "ref_total": float(r.total_line), "ref_over_price": int(r.over_price),
            "ref_under_price": int(r.under_price), "ref_p_over_novig": round(devig_over(r.over_price, r.under_price), 4)}


def scoped(q: pd.DataFrame, scope: str) -> pd.DataFrame:
    if scope == "all_observed":
        return q
    if scope == NY_ACTIONABLE:
        return q[q.book.map(group_of) == NY_ACTIONABLE]
    raise ValueError(scope)


def build_line_shopping(quotes: pd.DataFrame) -> pd.DataFrame:
    """quotes: quotes_v2-shaped frame. Pregame rows only (in-play lines are excluded)."""
    if quotes.empty:
        return pd.DataFrame()
    flag = "is_pregame_game" if "is_pregame_game" in quotes else "is_pregame"
    q = quotes[quotes[flag].astype(str).str.lower() == "true"].copy()
    q["game_key"] = q.apply(game_key, axis=1)
    rows = []
    for (sid, gk), g in q.groupby(["snapshot_id", "game_key"], sort=True):
        first = g.iloc[0]
        row = {"snapshot_id": sid, "snapshot_ts_utc": first.snapshot_ts_utc, "snapshot_kind": first.snapshot_kind,
               "game_key": gk, "game_id": first.game_id, "home_team": first.home_team, "away_team": first.away_team,
               "kickoff_utc_resolved": first.get("kickoff_game", first.kickoff_utc_resolved),
               "minutes_to_kickoff": first.get("minutes_to_kickoff_game", first.minutes_to_kickoff)}
        for prefix, scope in (("ny_", NY_ACTIONABLE), ("all_", "all_observed")):
            row.update({prefix + k: v for k, v in shop_metrics(scoped(g, scope)).items()})
        row.update(reference_metrics(g))
        if row.get("ref_book") and row.get("ny_n_books"):
            row["ny_best_over_vs_ref_pts"] = round(row["ref_total"] - row["ny_best_over_total"], 2)
            row["ny_best_under_vs_ref_pts"] = round(row["ny_best_under_total"] - row["ref_total"], 2)
        rows.append(row)
    return pd.DataFrame(rows)


def shopping_frequencies(ls: pd.DataFrame, prefix: str) -> dict:
    """Gate B summary for one scope: % of (game, snapshot) rows with a >= 0.5 / 1.0 / 1.5 point
    better number than the canonical line, per side. Descriptive only."""
    d = ls[ls.get(f"{prefix}n_books", 0) > 0] if len(ls) else ls
    out = {"rows": int(len(d))}
    for side in ("over", "under"):
        col = f"{prefix}{side}_line_adv_pts"
        for t in C.SHOP_ADV_THRESHOLDS:
            out[f"{side}_pct_adv_ge_{t}"] = None if not len(d) else round(float((d[col] >= t).mean()), 4)
    return out
