"""Phase 5A derived spread tables: line shopping and first/last observed movement (research only, 0 units).

Built from paired spread quotes (src/nfl/data/odds_snapshot.parse_spread_quotes) after the same game-level
kickoff policy as totals (movement.apply_game_kickoff). Pregame rows only. Conventions:

  home_spread  the home team's number (-3.5 = home favored by 3.5). away_spread = -home_spread for each book.
  Best HOME number = the largest home_spread (more points for the home side), then the best home price at it.
  Best AWAY number = the largest away_spread, then the best away price at it. Ties on price -> book name.
  Consensus = canonical home number (most common posted number; a number some book actually posted).

Key numbers (3 and 7 first): `*_crosses_key` is true when the best number available is on the better side of a
key number compared with the consensus, for example home +3.5 when the consensus is home +3.

No CLV ledger yet: there are no spread picks to grade until a spread model passes its test (Phase 5B).
"""
from __future__ import annotations

import statistics

import pandas as pd

from src.nfl.market import phase4_config as C
from src.nfl.market.books import NY_ACTIONABLE, group_of, reference_books
from src.nfl.market.line_shopping import game_key
from src.nfl.market.odds_math import canonical_line, cents
from src.nfl.market.timing import close_quality

SCOPES = (("ny_", NY_ACTIONABLE), ("all_", "all_observed"))


def _best(q: pd.DataFrame, side: str) -> dict:
    num, price = (q.home_spread, "home_price") if side == "home" else (q.away_spread, "away_price")
    line = float(num.max())
    at = q[num == line].copy()
    at["_c"] = at[price].map(cents)
    top = at[at._c == at._c.max()].sort_values("book")
    r = top.iloc[0]
    return {"line": line, "price": int(r[price]), "book": r["book"], "books_at_line": ",".join(sorted(at.book))}


def _crosses(best: float, consensus: float) -> bool:
    """Best number is on the better side of a key margin vs consensus (works for either side's numbers):
    for a team getting points, +3.5 vs +3 crosses 3; for a favorite, -2.5 vs -3 crosses 3."""
    if best <= consensus:
        return False
    return any(consensus <= k < best or consensus <= -k < best for k in C.SPREAD_KEY_NUMBERS)


def spread_shop_metrics(q: pd.DataFrame) -> dict:
    n = int(len(q))
    if n == 0:
        return {"n_books": 0}
    homes = [float(x) for x in q.home_spread]
    can = canonical_line(homes)
    bh, ba = _best(q, "home"), _best(q, "away")
    return {"n_books": n, "median_home_spread": statistics.median(homes), "canonical_home_spread": can,
            "best_home_spread": bh["line"], "best_home_price": bh["price"], "best_home_book": bh["book"],
            "best_home_books_at_line": bh["books_at_line"],
            "best_away_spread": ba["line"], "best_away_price": ba["price"], "best_away_book": ba["book"],
            "best_away_books_at_line": ba["books_at_line"],
            "home_line_adv_pts": round(bh["line"] - can, 2), "away_line_adv_pts": round(ba["line"] + can, 2),
            "line_range": round(max(homes) - min(homes), 2), "n_distinct_lines": len(set(homes)),
            "home_crosses_key": _crosses(bh["line"], can), "away_crosses_key": _crosses(ba["line"], -can)}


def build_spread_line_shopping(squotes: pd.DataFrame) -> pd.DataFrame:
    """One row per (snapshot, game) over pregame paired spread quotes, by scope, plus the Pinnacle reference."""
    if squotes is None or squotes.empty:
        return pd.DataFrame()
    flag = "is_pregame_game" if "is_pregame_game" in squotes else "is_pregame"
    q = squotes[squotes[flag].astype(str).str.lower() == "true"].copy()
    if q.empty:
        return pd.DataFrame()
    q["game_key"] = q.apply(game_key, axis=1)
    refs = set(reference_books())
    rows = []
    for (sid, gk), g in q.groupby(["snapshot_id", "game_key"], sort=True):
        first = g.iloc[0]
        row = {"snapshot_id": sid, "snapshot_ts_utc": first.snapshot_ts_utc, "snapshot_kind": first.snapshot_kind,
               "game_key": gk, "game_id": first.game_id, "home_team": first.home_team, "away_team": first.away_team,
               "kickoff_utc": first.get("kickoff_game", first.kickoff_utc_resolved),
               "minutes_to_kickoff": first.get("minutes_to_kickoff_game", first.minutes_to_kickoff),
               "kickoff_status": first.get("kickoff_game_status", first.kickoff_status)}
        for prefix, scope in SCOPES:
            sq = g if scope == "all_observed" else g[g.book.map(group_of) == NY_ACTIONABLE]
            row.update({prefix + k: v for k, v in spread_shop_metrics(sq).items()})
        ref = g[g.book.isin(refs)]
        if len(ref):
            r = ref.sort_values("book").iloc[0]
            row.update({"ref_book": r.book, "ref_home_spread": float(r.home_spread),
                        "ref_home_price": int(r.home_price), "ref_away_price": int(r.away_price)})
        rows.append(row)
    return pd.DataFrame(rows)


def build_spread_movement(ls: pd.DataFrame, finals: dict | None = None) -> pd.DataFrame:
    """Per game: first observed and last observed pregame consensus home number (all observed books),
    move in points (positive = moved toward the home side getting more points), key numbers crossed by the move,
    close_quality (0-30 min before a RESOLVED kickoff with 5+ books), and the actual home margin when final."""
    if ls is None or ls.empty:
        return pd.DataFrame()
    out = []
    for gk, g in ls.sort_values("snapshot_ts_utc").groupby("game_key"):
        f, l = g.iloc[0], g.iloc[-1]
        a, b = f.all_canonical_home_spread, l.all_canonical_home_spread
        lo, hi = sorted((a, b))
        # a move touches a key number when it starts on, ends on, or passes through +/-k (for example -3 -> -3.5)
        crossed = [k for k in C.SPREAD_KEY_NUMBERS if lo <= k <= hi or lo <= -k <= hi] if a != b else []
        out.append({"game_key": gk, "game_id": l.game_id, "home_team": l.home_team, "away_team": l.away_team,
                    "n_snapshots": int(len(g)), "first_snapshot_id": f.snapshot_id,
                    "first_observed_home_spread": a, "last_pregame_snapshot_id": l.snapshot_id,
                    "last_observed_pregame_home_spread": b, "last_minutes_to_kickoff": l.minutes_to_kickoff,
                    "move_pts": round(b - a, 2), "key_numbers_crossed": ",".join(str(k) for k in crossed),
                    "close_quality": close_quality(l.minutes_to_kickoff, l.all_n_books, l.kickoff_status),
                    "actual_home_margin": (finals or {}).get(l.game_id)})
    return pd.DataFrame(out)
