"""Phase 4 CLV (spec section 9). Line CLV and price CLV are separate; no combined metric.

Reference snapshot R = the game's last_observed_pregame. Action snapshot S must be strictly
earlier than R. All "sides" here are hypothetical research rows with 0 units.

  OVER : line_clv = ref_line(R) - line_taken
  UNDER: line_clv = line_taken - ref_line(R)
  ref_line(R) = canonical total across ALL observed books at R (median at R reported too).

Price CLV is computed ONLY at an identical total number:
  price_clv_same_book            the taken book posts exactly line_taken at R
  price_clv_same_line_consensus  >= 3 books post exactly line_taken at R (median price)
  fair_ev_vs_ref                 p_fair(R, line_taken) * decimal(price_taken) - 1, >= 3 books at that line
Otherwise those fields are blank.
"""
from __future__ import annotations

import statistics

import pandas as pd

from src.nfl.market import phase4_config as C
from src.nfl.market.books import NY_ACTIONABLE, REFERENCE_SHARP, group_of
from src.nfl.market.line_shopping import _best, scoped
from src.nfl.market.odds_math import canonical_line, decimal_odds, devig_over, implied_prob, median_price
from src.nfl.market.timing import close_quality, parse_utc

VARIANTS = (("best_available_ny", NY_ACTIONABLE), ("best_available_all", "all_observed"), ("consensus_all", "all_observed"))
CLV_FIELDS = ["game_key", "game_id", "side", "variant", "action_snapshot_id", "action_ts", "action_kind",
              "action_minutes_to_kickoff", "line_taken", "price_taken", "book_taken", "book_taken_group",
              "ref_snapshot_id", "ref_ts", "ref_minutes_to_kickoff", "ref_n_books", "ref_close_quality", "kickoff_status",
              "kickoff_source",
              "ref_canonical_line", "ref_median_line", "ref_ny_canonical_line", "ref_book_line",
              "line_clv", "line_clv_vs_median", "line_clv_vs_ny_canonical", "line_clv_vs_ref_book",
              "price_clv_same_book", "price_clv_same_line_consensus", "fair_ev_vs_ref",
              "action_line_range", "dispersion_bucket", "move_s_to_r"]


def line_clv(side: str, line_taken: float, ref_line: float | None) -> float | None:
    if ref_line is None or line_taken is None or side not in ("OVER", "UNDER"):
        return None
    return round(float(ref_line - line_taken) if side == "OVER" else float(line_taken - ref_line), 2)


def price_clv(price_taken: float | None, ref_price: float | None) -> float | None:
    """implied(ref) - implied(taken); positive = the taken price was better. Caller guarantees
    both prices are at the SAME total number."""
    a, b = implied_prob(price_taken), implied_prob(ref_price)
    return None if a is None or b is None else round(b - a, 4)


def taken_quote(q: pd.DataFrame, side: str, variant: str, scope: str) -> dict | None:
    qs = scoped(q, scope)
    if qs.empty:
        return None
    if variant.startswith("best_available"):
        b = _best(qs, side)
        return {"line": b["line"], "price": b["price"], "book": b["book"]}
    can = canonical_line(list(qs.total_line))
    at = qs[qs.total_line == can]
    col = "over_price" if side == "OVER" else "under_price"
    return {"line": float(can), "price": median_price(list(at[col])), "book": None}


def same_line_price_clvs(r: pd.DataFrame, side: str, taken: dict) -> dict:
    col = "over_price" if side == "OVER" else "under_price"
    out = {"price_clv_same_book": None, "price_clv_same_line_consensus": None, "fair_ev_vs_ref": None}
    if taken["price"] is None:
        return out
    if taken["book"] is not None:
        sb = r[(r.book == taken["book"]) & (r.total_line == taken["line"])]
        if len(sb) == 1:
            out["price_clv_same_book"] = price_clv(taken["price"], sb[col].iloc[0])
    at = r[r.total_line == taken["line"]]
    if len(at) >= C.SAME_LINE_CONSENSUS_MIN_BOOKS:
        out["price_clv_same_line_consensus"] = price_clv(taken["price"], median_price(list(at[col])))
        p_over = statistics.fmean(devig_over(o, u) for o, u in zip(at.over_price, at.under_price))
        p_side = p_over if side == "OVER" else 1 - p_over
        out["fair_ev_vs_ref"] = round(p_side * decimal_odds(taken["price"]) - 1, 4)
    return out


def _bucket(value, buckets) -> str | None:
    if value is None or pd.isna(value):
        return None
    for lo, hi, label in buckets:
        if lo <= value < hi:
            return label
    return None


def build_clv_events(quotes: pd.DataFrame) -> pd.DataFrame:
    """quotes: game-kickoff-applied quotes (movement.apply_game_kickoff). One row per
    (game, action snapshot S < R, side, variant)."""
    if quotes.empty:
        return pd.DataFrame(columns=CLV_FIELDS)
    q = quotes[quotes.is_pregame_game.astype(str).str.lower() == "true"].copy()
    q["_ts"] = q.snapshot_ts_utc.map(parse_utc)
    rows = []
    for gk, g in q.groupby("game_key", sort=True):
        snaps = g.drop_duplicates("snapshot_id").sort_values("_ts")
        if len(snaps) < 2:
            continue
        rsnap = snaps.iloc[-1]
        r = g[g.snapshot_id == rsnap.snapshot_id]
        ref_can = canonical_line(list(r.total_line))
        ref_med = statistics.median(float(x) for x in r.total_line)
        rny = scoped(r, NY_ACTIONABLE)
        ref_ny = canonical_line(list(rny.total_line)) if len(rny) else None
        rref = r[r.book.map(group_of) == REFERENCE_SHARP]
        ref_book_line = float(rref.total_line.iloc[0]) if len(rref) else None
        r_mtk = float(rsnap.minutes_to_kickoff_game)
        k_status, k_source = rsnap.get("kickoff_game_status"), rsnap.get("kickoff_game_source")
        for s in snaps.iloc[:-1].itertuples():
            sq = g[g.snapshot_id == s.snapshot_id]
            s_can = canonical_line(list(sq.total_line))
            rng = float(sq.total_line.max() - sq.total_line.min())
            for side in ("OVER", "UNDER"):
                for variant, scope in VARIANTS:
                    t = taken_quote(sq, side, variant, scope)
                    if t is None:
                        continue
                    rows.append({
                        "game_key": gk, "game_id": s.game_id, "side": side, "variant": variant,
                        "action_snapshot_id": s.snapshot_id, "action_ts": s.snapshot_ts_utc, "action_kind": s.snapshot_kind,
                        "action_minutes_to_kickoff": float(s.minutes_to_kickoff_game),
                        "line_taken": t["line"], "price_taken": t["price"], "book_taken": t["book"],
                        "book_taken_group": group_of(t["book"]) if t["book"] else None,
                        "ref_snapshot_id": rsnap.snapshot_id, "ref_ts": rsnap.snapshot_ts_utc,
                        "ref_minutes_to_kickoff": r_mtk, "ref_n_books": int(len(r)),
                        "ref_close_quality": close_quality(r_mtk, int(len(r)), k_status),
                        "kickoff_status": k_status, "kickoff_source": k_source,
                        "ref_canonical_line": ref_can, "ref_median_line": ref_med, "ref_ny_canonical_line": ref_ny,
                        "ref_book_line": ref_book_line,
                        "line_clv": line_clv(side, t["line"], ref_can),
                        "line_clv_vs_median": line_clv(side, t["line"], ref_med),
                        "line_clv_vs_ny_canonical": line_clv(side, t["line"], ref_ny),
                        "line_clv_vs_ref_book": line_clv(side, t["line"], ref_book_line),
                        **same_line_price_clvs(r, side, t),
                        "action_line_range": rng, "dispersion_bucket": _bucket(rng, C.DISPERSION_BUCKETS),
                        "move_s_to_r": round(float(ref_can - s_can), 2),
                    })
    return pd.DataFrame(rows, columns=CLV_FIELDS)


def summarize_clv(ev: pd.DataFrame, by: list[str] | None = None) -> pd.DataFrame:
    """Descriptive CLV stats. To avoid overweighting games, keeps ONE row per (game, side, variant,
    action_kind): the earliest action snapshot of that kind."""
    if ev.empty:
        return pd.DataFrame()
    d = ev.sort_values("action_ts").drop_duplicates(["game_key", "side", "variant", "action_kind"])
    keys = ["variant", "side"] + (by or [])
    def agg(x: pd.DataFrame) -> pd.Series:
        c = x.line_clv.dropna()
        return pd.Series({"n": int(len(c)), "mean_line_clv": round(c.mean(), 3) if len(c) else None,
                          "median_line_clv": float(c.median()) if len(c) else None,
                          "pct_positive": round(float((c > 0).mean()), 4) if len(c) else None,
                          "pct_ge_0_5": round(float((c >= 0.5).mean()), 4) if len(c) else None,
                          "pct_ge_1_0": round(float((c >= 1.0).mean()), 4) if len(c) else None,
                          "n_price_clv_same_line": int(x.price_clv_same_line_consensus.notna().sum())})
    return d.groupby(keys, dropna=False).apply(agg, include_groups=False).reset_index()


def edge_bucket(edge: float | None) -> str | None:
    return _bucket(None if edge is None else abs(edge), C.EDGE_BUCKETS)
