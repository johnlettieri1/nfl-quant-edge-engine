"""Phase 5C: weekly spread sheet support (spec: docs/phase5/PHASE_5C_SPEC.md, approved Nov 3 2026).

LINE SHOPPING PLUS INJURY CHECKS, NOT A SPREAD MODEL. Phase 5B (PR #10) found no model beats the closing spread.
Nothing here predicts a margin; it compares John's pick-sheet lines with the spread quotes we already collect
(Phase 5A, no extra credits) and settles the picks afterwards so we can measure, honestly and slowly, whether
taking the better side of a stale or key-number sheet line covers more than 50%. Units stay 0.

Conventions (same as Phase 5A spread tables): `home_spread` is the home team's number, -3.5 = home favored by
3.5. A sheet row gives the favorite and a positive line L; its home_spread is -L if the home team is the
favorite, else +L. A pick's `pick_home_spread` is the sheet's home_spread; its side is "home" or "away".

Files:
  input sheet CSV (transcribed by hand): sheet_order, away_team, home_team, fav_team, line
      optional pick columns for writing a card: pick_team, confidence, favorite_pick, note
  card CSV (immutable, one per week): data/predictions/sheet/sheet_<season>_wk<NN>_<check ts>.csv
  ledger (append-only): data/predictions/sheet_ledger.csv
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.nfl.data.odds_snapshot import load_spread_quotes, write_exclusive, append_rows_atomic
from src.nfl.market.books import NY_ACTIONABLE, in_scope, reference_books

SHEET_DIR = Path("data/predictions/sheet")
SHEET_LEDGER = Path("data/predictions/sheet_ledger.csv")
PRIMARY_BOOK = "draftkings"
MAJOR_KEYS = (3, 7)
MINOR_KEYS = (6, 10, 4, 14)
STALE_PTS = 1.5
CONFIDENCE = ("High", "Medium", "Low")
# common sheet / sportsbook spellings -> nflverse 2026 codes (nflverse uses LA for the Rams, WAS, JAX, LV, ...)
SHEET_ALIASES = {"LAR": "LA", "WSH": "WAS", "JAC": "JAX", "LVR": "LV", "OAK": "LV", "SD": "LAC", "STL": "LA",
                 "ARZ": "ARI", "NWE": "NE", "NOR": "NO", "TAM": "TB", "KAN": "KC", "SFO": "SF", "GNB": "GB",
                 "HST": "HOU", "BLT": "BAL", "CLV": "CLE"}
TEAM_COLS = ("away_team", "home_team", "fav_team", "pick_team")


def normalize_sheet(sheet: pd.DataFrame) -> pd.DataFrame:
    s = sheet.copy()
    for c in TEAM_COLS:
        if c in s.columns:
            s[c] = s[c].map(lambda x: x if pd.isna(x) else SHEET_ALIASES.get(str(x).strip().upper(), str(x).strip().upper()))
    bad = s[(s.fav_team != s.home_team) & (s.fav_team != s.away_team)]
    if len(bad):
        raise ValueError(f"fav_team is neither team on sheet rows {bad.sheet_order.tolist()}")
    return s

CARD_FIELDS = ["season", "week", "game_id", "sheet_order", "away_team", "home_team", "fav_team", "sheet_line",
               "sheet_home_spread", "pick_team", "pick_side", "pick_line_display", "confidence", "favorite_pick",
               "check_ts_utc", "dk_snapshot_id", "dk_home_spread_at_check", "first_dk_home_spread",
               "best_ny_home_spread", "best_ny_away_spread", "pinnacle_home_spread", "edge_side", "edge_pts",
               "pick_edge_pts", "key_flag", "minor_key_flag", "stale_flag", "rule_confidence", "dk_source", "backfill", "note"]
LEDGER_FIELDS = ["card_file", "season", "week", "game_id", "away_team", "home_team", "pick_team", "pick_side",
                 "sheet_home_spread", "dk_home_spread_at_check", "dk_home_spread_close", "close_snapshot_id",
                 "home_score", "away_score", "margin", "result_sheet", "result_dk_check", "result_dk_close",
                 "pick_edge_pts", "key_flag", "stale_flag", "confidence", "favorite_pick", "backfill", "settled_at"]


# ----------------------------------------------------------------------------- pure helpers

def sheet_home_spread(home_team: str, fav_team: str, line: float) -> float:
    if line < 0:
        raise ValueError("sheet line must be given as a positive number for the favorite")
    return -float(line) if fav_team == home_team else float(line)


def cover(margin: float, home_spread: float, side: str) -> str:
    """'W', 'L' or 'P' for the side taking `home_spread` (home side) or -home_spread (away side)."""
    v = margin + home_spread
    if side == "away":
        v = -v
    return "W" if v > 0 else ("L" if v < 0 else "P")


def edge(sheet_hs: float, market_hs: float) -> tuple[str | None, float]:
    """Which side the sheet line favors versus the market, and by how many points (0 -> no side)."""
    d = sheet_hs - market_hs
    if d == 0:
        return None, 0.0
    return ("home" if d > 0 else "away"), abs(d)


def key_crossed(sheet_hs: float, market_hs: float, keys=MAJOR_KEYS) -> list[int]:
    """Key margins k where the side the sheet favors wins at the sheet number but does not win at the market
    number (for example Vikings +3.5 on the sheet vs +3 at DraftKings: a 3-point loss covers only the sheet)."""
    side, _ = edge(sheet_hs, market_hs)
    if side is None:
        return []
    hit = []
    for k in keys:
        for m in (k, -k):
            if cover(m, sheet_hs, side) == "W" and cover(m, market_hs, side) != "W":
                hit.append(k)
                break
    return hit


def rule_confidence(pick_edge_pts: float | None, crosses_major_key: bool, qb_question: bool = False) -> str:
    """Spec section 4 (with clarification A1): High = pick gets 1.5+ points versus DraftKings; Medium = 0.5 to 1.0
    points that cross 3 or 7, or 1.0+ points that do not; Low = anything else. A questionable or doubtful
    starting QB in the game caps the label at Low. Labels only: not probabilities, no edge claim."""
    if qb_question or pick_edge_pts is None or pick_edge_pts <= 0:
        return "Low"
    if pick_edge_pts >= 1.5:
        return "High"
    if crosses_major_key or pick_edge_pts >= 1.0:
        return "Medium"
    return "Low"


def display_line(team_hs: float) -> str:
    return "PK" if team_hs == 0 else f"{team_hs:+g}"


# ----------------------------------------------------------------------------- quotes

def _pregame(quotes: pd.DataFrame) -> pd.DataFrame:
    q = quotes.copy()
    for c in ("home_spread", "away_spread", "minutes_to_kickoff"):
        q[c] = pd.to_numeric(q[c], errors="coerce")
    q["snapshot_ts"] = pd.to_datetime(q["snapshot_ts_utc"], utc=True, format="mixed")
    pre = q["is_pregame"].astype(str).str.lower().isin(("true", "1")) & (q["minutes_to_kickoff"] > 0)
    return q[pre & q["home_spread"].notna()]


def _match(q: pd.DataFrame, away: str, home: str) -> pd.DataFrame:
    return q[(q.home_team == home) & (q.away_team == away)]


def market_view(q: pd.DataFrame, away: str, home: str, as_of: datetime | None = None) -> dict:
    """Market numbers for one game from snapshots at or before `as_of` (pregame only)."""
    g = _match(q, away, home)
    if as_of is not None:
        g = g[g.snapshot_ts <= pd.Timestamp(as_of)]
    out = {"game_id": None, "dk_snapshot_id": None, "dk_home_spread_at_check": None, "first_dk_home_spread": None,
           "best_ny_home_spread": None, "best_ny_away_spread": None, "pinnacle_home_spread": None}
    if g.empty:
        return out
    out["game_id"] = g.game_id.dropna().iloc[-1] if g.game_id.notna().any() else None
    dk = g[g.book == PRIMARY_BOOK].sort_values("snapshot_ts")
    if not dk.empty:
        out["first_dk_home_spread"] = float(dk.iloc[0].home_spread)
        out["dk_home_spread_at_check"] = float(dk.iloc[-1].home_spread)
        out["dk_snapshot_id"] = dk.iloc[-1].snapshot_id
    last = g[g.snapshot_ts == g.snapshot_ts.max()]
    ny = last[last.book.map(lambda b: in_scope(b, NY_ACTIONABLE))]
    if not ny.empty:
        out["best_ny_home_spread"] = float(ny.home_spread.max())
        out["best_ny_away_spread"] = float(ny.away_spread.max())
    ref = last[last.book.isin(reference_books())]
    if not ref.empty:
        out["pinnacle_home_spread"] = float(ref.iloc[0].home_spread)
    return out


def last_pregame_dk(q: pd.DataFrame, game_id: str) -> tuple[float | None, str | None]:
    dk = q[(q.game_id == game_id) & (q.book == PRIMARY_BOOK)].sort_values("snapshot_ts")
    if dk.empty:
        return None, None
    r = dk.iloc[-1]
    return float(r.home_spread), r.snapshot_id


# ----------------------------------------------------------------------------- check / card

def check_sheet(sheet: pd.DataFrame, quotes: pd.DataFrame, as_of: datetime | None = None) -> pd.DataFrame:
    sheet = normalize_sheet(sheet)
    q = _pregame(quotes)
    rows = []
    for r in sheet.sort_values("sheet_order").itertuples(index=False):
        shs = sheet_home_spread(r.home_team, r.fav_team, float(r.line))
        m = market_view(q, r.away_team, r.home_team, as_of)
        m["dk_source"] = "snapshot" if m["dk_home_spread_at_check"] is not None else ""
        if m["game_id"] is None:                          # game id only (never a line) from any later snapshot
            m["game_id"] = market_view(q, r.away_team, r.home_team)["game_id"]
        hand = getattr(r, "dk_fav_line", None)
        if hand is not None and not pd.isna(hand) and str(hand).strip():
            # backfill only: DraftKings number documented on the saved card before kickoff (hand-read)
            m["dk_home_spread_at_check"] = sheet_home_spread(r.home_team, r.fav_team, float(hand))
            m["dk_snapshot_id"], m["dk_source"] = "", "card_web"
        row = {"sheet_order": int(r.sheet_order), "away_team": r.away_team, "home_team": r.home_team,
               "fav_team": r.fav_team, "sheet_line": float(r.line), "sheet_home_spread": shs, **m}
        dk = m["dk_home_spread_at_check"]
        if dk is None:
            row.update(edge_side=None, edge_pts=None, key_flag="", minor_key_flag="", stale_flag=False)
        else:
            side, pts = edge(shs, dk)
            row.update(edge_side=side, edge_pts=pts,
                       key_flag=",".join(map(str, key_crossed(shs, dk, MAJOR_KEYS))),
                       minor_key_flag=",".join(map(str, key_crossed(shs, dk, MINOR_KEYS))),
                       stale_flag=bool(pts >= STALE_PTS))
        rows.append(row)
    return pd.DataFrame(rows)


def format_table(chk: pd.DataFrame) -> str:
    def team_num(team_hs, home, away, team):
        return display_line(team_hs if team == home else -team_hs)
    lines = ["# | Game | Sheet | DK now | DK first | Best NY (fav side / dog side) | Pinnacle | Sheet edge | Flags"]
    for r in chk.itertuples(index=False):
        fav, dog = r.fav_team, (r.away_team if r.fav_team == r.home_team else r.home_team)
        sheet = f"{fav} {team_num(r.sheet_home_spread, r.home_team, r.away_team, fav)}"
        def num(hs):
            return "n/a" if hs is None or pd.isna(hs) else f"{fav} {team_num(hs, r.home_team, r.away_team, fav)}"
        if r.best_ny_home_spread is None or pd.isna(r.best_ny_home_spread):
            ny = "n/a"
        else:
            fav_best = r.best_ny_home_spread if fav == r.home_team else r.best_ny_away_spread
            dog_best = r.best_ny_away_spread if fav == r.home_team else r.best_ny_home_spread
            ny = f"{fav} {display_line(fav_best)} / {dog} {display_line(dog_best)}"
        if r.edge_side is None or (isinstance(r.edge_side, float) and pd.isna(r.edge_side)):
            e = "none" if r.dk_home_spread_at_check is not None and not pd.isna(r.dk_home_spread_at_check) else "no DK quote"
        else:
            team = r.home_team if r.edge_side == "home" else r.away_team
            e = f"+{r.edge_pts:g} for {team}"
        flags = []
        if r.key_flag:
            flags.append(f"crosses {r.key_flag}")
        if r.minor_key_flag:
            flags.append(f"minor {r.minor_key_flag}")
        if r.stale_flag:
            flags.append("STALE")
        lines.append(f"{r.sheet_order} | {r.away_team} @ {r.home_team} | {sheet} | {num(r.dk_home_spread_at_check)} | "
                     f"{num(r.first_dk_home_spread)} | {ny} | {num(r.pinnacle_home_spread)} | {e} | {' '.join(flags)}")
    return "\n".join(lines)


def build_card(sheet: pd.DataFrame, chk: pd.DataFrame, season: int, week: int, check_ts: datetime,
               backfill: bool = False) -> pd.DataFrame:
    """Merge picks (pick_team, confidence, favorite_pick, note) into the check rows; validates every pick."""
    s = normalize_sheet(sheet).set_index("sheet_order")
    favs = 0
    rows = []
    for r in chk.itertuples(index=False):
        p = s.loc[r.sheet_order]
        pick = str(p["pick_team"]).strip()
        if pick not in (r.home_team, r.away_team):
            raise ValueError(f"row {r.sheet_order}: pick {pick!r} is not {r.away_team} or {r.home_team}")
        conf = str(p.get("confidence", "")).strip()
        side_ = "home" if pick == r.home_team else "away"
        dk_ = r.dk_home_spread_at_check
        pe_ = None if dk_ is None or pd.isna(dk_) else (r.sheet_home_spread - dk_ if side_ == "home" else dk_ - r.sheet_home_spread)
        qbq = str(p.get("qb_question", "")).strip().lower() in ("1", "true", "yes", "y")
        crosses = bool(r.key_flag) and r.edge_side == side_
        rule = rule_confidence(pe_, crosses, qbq)
        if conf in ("", "nan", "rule"):
            conf = rule
        if conf not in CONFIDENCE:
            raise ValueError(f"row {r.sheet_order}: confidence must be one of {CONFIDENCE}")
        fp = str(p.get("favorite_pick", "")).strip().lower() in ("1", "true", "yes", "y")
        favs += fp
        side = "home" if pick == r.home_team else "away"
        dk = r.dk_home_spread_at_check
        has_dk = dk is not None and not pd.isna(dk)
        pick_edge = (r.sheet_home_spread - dk if side == "home" else dk - r.sheet_home_spread) if has_dk else None
        pick_hs = r.sheet_home_spread if side == "home" else -r.sheet_home_spread
        note = p.get("note", "")
        rows.append({"season": season, "week": week, "game_id": r.game_id or "", "sheet_order": r.sheet_order,
                     "away_team": r.away_team, "home_team": r.home_team, "fav_team": r.fav_team,
                     "sheet_line": r.sheet_line, "sheet_home_spread": r.sheet_home_spread, "pick_team": pick,
                     "pick_side": side, "pick_line_display": display_line(pick_hs), "confidence": conf,
                     "favorite_pick": fp, "check_ts_utc": check_ts.astimezone(timezone.utc).isoformat(),
                     "dk_snapshot_id": r.dk_snapshot_id or "", "dk_home_spread_at_check": dk if has_dk else "",
                     "first_dk_home_spread": "" if r.first_dk_home_spread is None else r.first_dk_home_spread,
                     "best_ny_home_spread": "" if r.best_ny_home_spread is None else r.best_ny_home_spread,
                     "best_ny_away_spread": "" if r.best_ny_away_spread is None else r.best_ny_away_spread,
                     "pinnacle_home_spread": "" if r.pinnacle_home_spread is None else r.pinnacle_home_spread,
                     "edge_side": r.edge_side or "", "edge_pts": "" if r.edge_pts is None else r.edge_pts,
                     "pick_edge_pts": "" if pick_edge is None else pick_edge, "key_flag": r.key_flag,
                     "minor_key_flag": r.minor_key_flag, "stale_flag": bool(r.stale_flag), "rule_confidence": rule,
                     "dk_source": r.dk_source, "backfill": backfill,
                     "note": "" if pd.isna(note) else str(note)})
    if favs != 1:
        raise ValueError(f"exactly one favorite_pick required, got {favs}")
    missing = [r["sheet_order"] for r in rows if not r["game_id"]]
    if missing:
        raise ValueError(f"no collected quotes matched sheet rows {missing}; check team codes or home/away order")
    return pd.DataFrame(rows, columns=CARD_FIELDS)


def write_card(card: pd.DataFrame, pred_dir: Path = SHEET_DIR.parent) -> Path:
    season, week = int(card.season.iloc[0]), int(card.week.iloc[0])
    ts = pd.Timestamp(card.check_ts_utc.iloc[0]).strftime("%Y%m%dT%H%M%SZ")
    path = pred_dir / "sheet" / f"sheet_{season}_wk{week:02d}_{ts}.csv"
    existing = sorted(path.parent.glob(f"sheet_{season}_wk{week:02d}_*.csv"))
    if existing:   # one card per week, so a week can never be settled twice; corrections need John's call
        raise FileExistsError(f"a card for {season} week {week} already exists: {existing[0].name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_exclusive(path, card.to_csv(index=False, lineterminator="\n").encode("utf-8"))


# ----------------------------------------------------------------------------- settlement

def _settled(ledger: Path) -> set[tuple[str, str]]:
    if not ledger.exists():
        return set()
    d = pd.read_csv(ledger, dtype=str)
    return set(zip(d.card_file, d.game_id))


def settle_sheets(schedules: pd.DataFrame, now: datetime, pred_dir: Path = SHEET_DIR.parent,
                  odds_dir: Path = Path("data/odds")) -> int:
    """Append-only, idempotent: settles every card row whose game is a verified final (same rule as totals)."""
    from src.nfl.reporting.ledger import is_verified_final
    cards = sorted((pred_dir / "sheet").glob("sheet_*.csv"))
    if not cards:
        return 0
    ledger = pred_dir / SHEET_LEDGER.name
    done = _settled(ledger)
    sched = schedules.set_index("game_id")
    q = _pregame(load_spread_quotes(odds_dir))
    new = []
    for path in cards:
        card = pd.read_csv(path, dtype={"game_id": str, "key_flag": str, "minor_key_flag": str,
                                        "pick_line_display": str, "dk_source": str})
        for r in card.itertuples(index=False):
            if (path.name, r.game_id) in done or r.game_id not in sched.index:
                continue
            g = sched.loc[r.game_id]
            if not is_verified_final(g.to_dict(), now):
                continue
            margin = float(g.home_score) - float(g.away_score)
            close, close_id = last_pregame_dk(q, r.game_id)
            dkc = None if pd.isna(r.dk_home_spread_at_check) else float(r.dk_home_spread_at_check)
            new.append({"card_file": path.name, "season": r.season, "week": r.week, "game_id": r.game_id,
                        "away_team": r.away_team, "home_team": r.home_team, "pick_team": r.pick_team,
                        "pick_side": r.pick_side, "sheet_home_spread": r.sheet_home_spread,
                        "dk_home_spread_at_check": "" if dkc is None else dkc,
                        "dk_home_spread_close": "" if close is None else close, "close_snapshot_id": close_id or "",
                        "home_score": int(g.home_score), "away_score": int(g.away_score), "margin": margin,
                        "result_sheet": cover(margin, float(r.sheet_home_spread), r.pick_side),
                        "result_dk_check": "" if dkc is None else cover(margin, dkc, r.pick_side),
                        "result_dk_close": "" if close is None else cover(margin, close, r.pick_side),
                        "pick_edge_pts": "" if pd.isna(r.pick_edge_pts) else r.pick_edge_pts,
                        "key_flag": "" if pd.isna(r.key_flag) else r.key_flag, "stale_flag": r.stale_flag,
                        "confidence": r.confidence, "favorite_pick": r.favorite_pick, "backfill": r.backfill,
                        "settled_at": now.astimezone(timezone.utc).isoformat()})
    return append_rows_atomic(ledger, new, LEDGER_FIELDS)


def wilson(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float, float]:
    """Wilson score interval (95% by default). Kept local: market code never imports research harnesses."""
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    p = k / n
    den = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return p, cen - half, cen + half


def summarize_sheet(ledger: Path = SHEET_LEDGER) -> dict:
    """Plain counts with a Wilson 95% interval. Reported, never tuned on."""
    if not ledger.exists():
        return {"games": 0}
    d = pd.read_csv(ledger)
    out = {"games": int(len(d))}

    def rate(sub: pd.DataFrame, col: str = "result_sheet") -> dict:
        x = sub[sub[col].isin(["W", "L"])]
        k, n = int((x[col] == "W").sum()), int(len(x))
        p, lo, hi = wilson(k, n)
        return {"w": k, "l": n - k, "p": int((sub[col] == "P").sum()), "rate": p, "ci95": [lo, hi]}
    out["all"] = rate(d)
    edge_rows = d[pd.to_numeric(d.pick_edge_pts, errors="coerce") > 0]
    out["pick_got_sheet_edge"] = rate(edge_rows)
    out["no_sheet_edge"] = rate(d[~d.index.isin(edge_rows.index)])
    out["sheet_edge_changed_result"] = int(((d.result_sheet == "W") & (d.result_dk_check != "W")).sum()
                                           - ((d.result_sheet != "W") & (d.result_dk_check == "W")).sum())
    out["by_confidence"] = {c: rate(d[d.confidence == c]) for c in CONFIDENCE}
    return out
