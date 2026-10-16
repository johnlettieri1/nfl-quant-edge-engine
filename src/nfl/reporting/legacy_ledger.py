"""Legacy pre-spec prediction settlement (Week 6 compatibility hotfix, approved Oct 16 2026).

The Oct 15 2026 Week 6 prediction snapshot (data/predictions/totals_2026_wk06_20261015T180406Z.json)
was produced by the Phase 2B code before Phase 4 existed. Its games reference the market as
`odds_snapshot:20261015T180351Z` (timestamp only), and its `market_total` is the MEDIAN across the 9
US-region books in that Phase 2B pull. That median is not a quote any sportsbook offered, and the
snapshot's `over_price` / `under_price` belong to the best line at possibly different books, so they
are not a price paired with `market_total`.

Such predictions cannot satisfy the Phase 4 executable-quote variants (`consensus_all`,
`best_available_ny`). They are therefore settled here, into a SEPARATE append-only file
`data/predictions/totals_ledger_legacy_pre_spec.csv`, as variant `legacy_pre_spec`:

  - the legacy market id is resolved deterministically to exactly one raw file on records (else the row
    is marked unresolved and gets no market comparison);
  - result = hypothetical research outcome against the ORIGINALLY recorded total, labeled
    `hypothetical_research_not_a_wager`; no later or better number is substituted;
  - no sportsbook, price or wager is manufactured: book_taken and price_taken stay blank, and
    price CLV is always blank (no executable price exists);
  - consensus_line_move: like-for-like movement of the all-books MEDIAN from the prediction's market
    number to the median at the game's last_observed_pregame snapshot, only when such a snapshot exists
    strictly after the legacy snapshot and strictly before kickoff. It is market movement, not executable
    CLV, and the reference book set is recorded because it differs from the legacy 9-book set;
  - fields the old snapshot never had (calibrated probability) stay blank;
  - the original snapshot is never modified, and units are always 0.

Phase 4 predictions keep the unchanged totals_ledger_v2 schema and path.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.nfl.market.clv import edge_bucket
from src.nfl.market.odds_math import canonical_line
from src.nfl.market.timing import close_quality, parse_raw_name, parse_utc

SCHEMA = "totals_ledger_legacy_pre_spec_v1"
VARIANT = "legacy_pre_spec"
RESULT_TYPE = "hypothetical_research_not_a_wager"
LEDGER_NAME = "totals_ledger_legacy_pre_spec.csv"
LEGACY_SOURCE_RE = re.compile(r"^odds_snapshot:(\d{8}T\d{6}Z)$")
FIELDS = ["game_id", "season", "week", "away_team", "home_team", "snapshot_file", "prediction_ts", "model_version",
          "variant", "result_type", "original_market_source", "source_resolution", "resolved_raw_file",
          "resolved_snapshot_id", "resolved_snapshot_ts", "side", "line_taken", "line_basis", "line_verified_against_raw",
          "original_n_books", "price_taken", "book_taken", "projected_total", "model_edge", "edge_bucket",
          "raw_model_probability", "calibrated_probability", "actual_total", "result",
          "ref_snapshot_id", "ref_ts", "ref_minutes_to_kickoff", "ref_n_books", "ref_kickoff_status", "ref_close_quality",
          "ref_median_line", "ref_canonical_line", "ref_book_set", "consensus_line_move", "price_clv",
          "units_risked", "recorded_at", "schema_version"]


def is_legacy_snapshot(snap: dict) -> bool:
    """A pre-spec prediction snapshot: at least one game references its market with the Phase 2B
    timestamp-only id `odds_snapshot:<YYYYmmddTHHMMSSZ>`. Phase 4 ids always carry the snapshot kind
    (`odds_snapshot:<ts>_<kind>`), so this rule is deterministic and never matches a Phase 4 snapshot."""
    return any(LEGACY_SOURCE_RE.match(str(g.get("market_source") or "")) for g in snap.get("games", []))


def resolve_legacy_source(market_source: str | None, odds_dir: Path) -> dict | None:
    """Map `odds_snapshot:<YYYYmmddTHHMMSSZ>` to exactly one raw file whose timestamp matches and whose
    envelope is the pre-spec (non odds_raw_v2) format. Returns None when there is no unique match."""
    m = LEGACY_SOURCE_RE.match(str(market_source or ""))
    if not m:
        return None
    ts = m.group(1)
    raw_dir = odds_dir / "raw"
    hits = []
    for p in sorted(raw_dir.glob(f"totals_{ts}_*.json")) if raw_dir.exists() else []:
        parsed = parse_raw_name(p.name)
        if parsed and parsed[0].strftime("%Y%m%dT%H%M%SZ") == ts:
            hits.append(p)
    if len(hits) != 1:
        return None
    env = json.loads(hits[0].read_text(encoding="utf-8"))
    if env.get("schema_version") == "odds_raw_v2":
        return None  # a Phase 4 snapshot is never the target of a legacy id
    return {"raw_file": hits[0].name, "snapshot_id": f"{ts}_legacy_current",
            "snapshot_dt": parse_utc(env["fetched_at"]), "games": env.get("games", [])}


def _reference(quotes: pd.DataFrame, game_id: str, after: datetime) -> dict:
    """Last pregame snapshot of the game strictly after `after` (game-kickoff-applied quotes)."""
    if quotes is None or quotes.empty:
        return {}
    g = quotes[(quotes.game_id == game_id) & (quotes.is_pregame_game.astype(str).str.lower() == "true")].copy()
    if g.empty:
        return {}
    g["_ts"] = g.snapshot_ts_utc.map(parse_utc)
    g = g[g._ts > after]
    if g.empty:
        return {}
    sid = g.sort_values("_ts").snapshot_id.iloc[-1]
    r = g[g.snapshot_id == sid]
    lines = [float(x) for x in r.total_line]
    mtk = float(r.minutes_to_kickoff_game.iloc[0])
    status = r.kickoff_game_status.iloc[0]
    return {"ref_snapshot_id": sid, "ref_ts": r.snapshot_ts_utc.iloc[0], "ref_minutes_to_kickoff": mtk,
            "ref_n_books": len(r), "ref_kickoff_status": status,
            "ref_close_quality": close_quality(mtk, len(r), status),
            "ref_median_line": statistics.median(lines), "ref_canonical_line": canonical_line(lines),
            "ref_book_set": ",".join(sorted(r.book))}


def _raw_median(games: list[dict], game: dict) -> float | None:
    """Median of the pre-spec raw book lines for this game (used only to verify the recorded total)."""
    from src.nfl.data.team_mapping import to_nflverse_code
    for ev in games:
        if to_nflverse_code(ev.get("home_team")) == game.get("home_team") and to_nflverse_code(ev.get("away_team")) == game.get("away_team"):
            lines = []
            for b in ev.get("bookmakers", []):
                for mk in b.get("markets", []):
                    if mk.get("key") != "totals":
                        continue
                    o = next((x for x in mk.get("outcomes", []) if x.get("name") == "Over"), None)
                    u = next((x for x in mk.get("outcomes", []) if x.get("name") == "Under"), None)
                    if o and u and "point" in o and "point" in u and float(o["point"]) == float(u["point"]):
                        lines.append(float(o["point"]))
            return statistics.median(lines) if lines else None
    return None


def build_legacy_rows(snap: dict, snap_file: str, game: dict, actual: float, quotes: pd.DataFrame,
                      odds_dir: Path, now: str) -> list[dict]:
    from src.nfl.reporting.ledger import settle   # local import: avoid a module cycle
    side = game.get("lean") if game.get("lean") in ("OVER", "UNDER") else "PASS"
    src = game.get("market_source")
    resolved = resolve_legacy_source(src, odds_dir)
    line = game.get("market_total")
    raw_med = _raw_median(resolved["games"], game) if resolved else None
    row = {"game_id": game["game_id"], "season": game.get("season"), "week": game.get("week"),
           "away_team": game.get("away_team"), "home_team": game.get("home_team"), "snapshot_file": snap_file,
           "prediction_ts": snap.get("generated_at"), "model_version": snap.get("model_version"),
           "variant": VARIANT, "result_type": RESULT_TYPE, "original_market_source": src,
           "source_resolution": "resolved" if resolved else "unresolved",
           "resolved_raw_file": resolved["raw_file"] if resolved else None,
           "resolved_snapshot_id": resolved["snapshot_id"] if resolved else None,
           "resolved_snapshot_ts": resolved["snapshot_dt"].isoformat() if resolved else None,
           "side": side, "line_taken": line,
           "line_basis": "legacy_median_all_observed_books_not_executable" if line is not None else None,
           "line_verified_against_raw": (raw_med is not None and line is not None and float(raw_med) == float(line)) if resolved else None,
           "original_n_books": game.get("n_books"), "price_taken": None, "book_taken": None,
           "projected_total": game.get("projected_total"), "model_edge": game.get("edge"),
           "edge_bucket": edge_bucket(game.get("edge")),
           "raw_model_probability": game.get("raw_model_probability", game.get("model_over_prob")),
           "calibrated_probability": game.get("calibrated_probability"),  # blank when the old snapshot never had it
           "actual_total": actual, "result": settle(side, None if line is None else float(line), actual),
           "price_clv": None, "units_risked": 0.0, "recorded_at": now, "schema_version": SCHEMA}
    if resolved and side in ("OVER", "UNDER") and line is not None:
        ref = _reference(quotes, game["game_id"], resolved["snapshot_dt"])
        row.update(ref)
        if ref:
            move = float(ref["ref_median_line"]) - float(line)
            row["consensus_line_move"] = round(move if side == "OVER" else -move, 2) + 0.0   # no negative zero
    return [row]
