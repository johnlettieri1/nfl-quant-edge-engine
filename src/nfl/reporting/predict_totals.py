"""Totals-only weekly prediction snapshot (Phase 2B).

Replaces reporting/predict.py in the weekly execution path. Moneyline, spread,
player props, and parlay code stay in the repo (research/history) but are never
imported here.

Pipeline:
  cached schedules + play-by-play (incremental cache, see data/ingest.py)
  -> repo feature builders, unchanged (trailing EPA 10, QB 10, scoring 16)
  -> production Ridge totals fit (models/total/production.py, unchanged)
  -> market: latest per-book odds snapshot for the game when one exists, else
     nflverse's schedule-embedded line and juice
  -> one immutable JSON snapshot: data/predictions/totals_<season>_wk<NN>_<UTC ts>.json

The snapshot records a model LEAN (Over/Under by sign of edge) and sets
recommended_units = 0. The Phase 2A baseline showed the lean carries no
demonstrated edge; the decision layer (edge thresholds, EV, Kelly, confidence,
PASS) is Phase 4 and is deliberately not here. Nothing is fabricated: a game
without a market line gets market_total = null and a data-quality flag.

Calibrated display (ridge8_v1_shrink_cal_v1, Oct 2026): every game also carries
raw_model_probability (identical to model_over_prob, which is kept unchanged) and
calibrated_probability (frozen shrink toward 50%, fit on seasons <= 2022 only; see
models/total/calibration.py). A human-readable .md summary beside each JSON snapshot shows
only the calibrated number, labeled as calibrated. Calibration never changes projected_total,
sigma, edge, lean or recommended_units. Snapshot files are created exclusively and are never
overwritten.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.nfl.data.ingest import load_cached_pbp_for_epa, load_cached_schedules
from src.nfl.data.odds_snapshot import load_game_summary
from src.nfl.features.epa_features import add_trailing_epa_features
from src.nfl.features.qb_features import add_trailing_qb_features
from src.nfl.models.total.calibration import apply_shrink, load_calibration, snapshot_metadata
from src.nfl.models.total.production import fit_and_predict_total
from src.nfl.models.total.total_model import FEATURE_COLS, MODEL_VERSION, over_probability

logger = logging.getLogger(__name__)
PRED_DIR = Path("data/predictions")


def target_week(games: pd.DataFrame) -> tuple[int, int]:
    unplayed = games[games.home_score.isna()].copy()
    if unplayed.empty:
        raise SystemExit("No unplayed games found.")
    unplayed["gameday"] = pd.to_datetime(unplayed.gameday)
    row = unplayed.sort_values(["season", "week", "gameday"]).iloc[0]
    return int(row.season), int(row.week)


def _market_for(game_id: str, sched: pd.Series, odds: pd.DataFrame | None, max_age_hours: float) -> dict:
    """Latest odds snapshot for the game if fresh, else nflverse line/juice."""
    if odds is not None and not odds.empty:
        rows = odds[odds.game_id == game_id].sort_values("snapshot_dt")
        if len(rows):
            r = rows.iloc[-1]
            age = (datetime.now(timezone.utc) - r.snapshot_dt.to_pydatetime()).total_seconds() / 3600
            if age <= max_age_hours:
                return {"market_total": float(r.market_median), "market_source": f"odds_snapshot:{r.snapshot_ts}",
                        "n_books": int(r.n_books), "lowest_total": float(r.lowest_total), "highest_total": float(r.highest_total),
                        "best_over_line": float(r.best_over_line), "best_over_price": int(r.best_over_price), "best_over_book": r.best_over_book,
                        "best_under_line": float(r.best_under_line), "best_under_price": int(r.best_under_price), "best_under_book": r.best_under_book,
                        "over_price": int(r.best_over_price), "under_price": int(r.best_under_price),
                        "consensus_over_prob": float(r.consensus_over_prob)}
    tl = sched.get("total_line")
    if tl is None or pd.isna(tl):
        return {"market_total": None, "market_source": None}
    oo, uo = sched.get("over_odds"), sched.get("under_odds")
    return {"market_total": float(tl), "market_source": "nflverse", "n_books": None,
            "over_price": None if pd.isna(oo) else int(oo), "under_price": None if pd.isna(uo) else int(uo)}


def _write_new(path: Path, text: str) -> None:
    """Exclusive create: an existing snapshot is never overwritten (raises FileExistsError)."""
    with open(path, "x", encoding="utf-8") as f:
        f.write(text)


def _pct(p) -> str:
    return "n/a" if p is None else f"{100 * p:.1f}%"


def render_summary(snapshot: dict) -> str:
    """Human-facing weekly view. Shows the calibrated (historical close-based) probability only; raw stays in the JSON."""
    cal = snapshot["probability_calibration"]
    lines = [f"# Totals leans: {snapshot['season']} Week {snapshot['week']:02d}", "",
             f"Generated {snapshot['generated_at']} | model {snapshot['model_version']} | recommended units: 0 (leans only)", "",
             f"**Probabilities shown are CALIBRATED (historical close-based)** ({cal['calibration_version']}: shrink toward "
             f"50%, factor {cal['shrink_factor']}, fit on seasons {cal['training_seasons'][0]}-{cal['training_seasons'][1]} only). "
             "The raw model probability is kept in the JSON snapshot for audit.", "",
             "Probability calibration was fitted against historical closing-line outcomes. It has not yet been "
             "independently validated for this snapshot's market timing.", "",
             "| Game | Date | Market total | Projected total | Edge | Lean | Calibrated P(Over), close-based | Calibrated P(lean), close-based |",
             "|---|---|---:|---:|---:|---|---:|---:|"]
    for g in snapshot["games"]:
        cp = g.get("calibrated_probability")
        lean_p = None if cp is None or g["lean"] not in ("OVER", "UNDER") else (cp if g["lean"] == "OVER" else round(1 - cp, 4))
        mt = "n/a" if g["market_total"] is None else f"{g['market_total']:.1f}"
        pt = "n/a" if g["projected_total"] is None else f"{g['projected_total']:.1f}"
        ed = "n/a" if g["edge"] is None else f"{g['edge']:+.1f}"
        lines.append(f"| {g['away_team']} @ {g['home_team']} | {g['gameday']} | {mt} | {pt} | {ed} | {g['lean'] or 'n/a'} | "
                     f"{_pct(cp)} | {_pct(lean_p)} |")
    lines += ["", "Not betting advice. ridge8_v1 has no demonstrated edge (Phase 2A, 3A, 3B); these are leans at 0 units.", ""]
    return "\n".join(lines)


def generate(season: int | None = None, week: int | None = None, pred_dir: Path = PRED_DIR,
             odds_max_age_hours: float = 48.0) -> Path:
    cal = load_calibration(model_version=MODEL_VERSION)   # fails closed on a model-version mismatch
    games = load_cached_schedules()
    games["gameday"] = pd.to_datetime(games.gameday)
    if season is None or week is None:
        season, week = target_week(games)
    logger.info("Totals slate: season=%d week=%d", season, week)

    pbp = load_cached_pbp_for_epa()
    g = add_trailing_epa_features(games, pbp, window=10)
    g = add_trailing_qb_features(g, pbp, window=10)
    df = fit_and_predict_total(g, window=16)
    df["over_prob"] = over_probability(df.total_mean_pred, df.total_sigma_pred, df.total_line)

    slate = df[(df.season == season) & (df.week == week) & df.home_score.isna()].copy()
    odds = load_game_summary()
    if not odds.empty:
        odds = odds[odds.game_id.notna()]
    records = []
    for row in slate.itertuples(index=False):
        r = row._asdict()
        mkt = _market_for(r["game_id"], pd.Series(r), odds, odds_max_age_hours)
        proj = None if pd.isna(r["total_mean_pred"]) else round(float(r["total_mean_pred"]), 2)
        sigma = None if pd.isna(r["total_sigma_pred"]) else round(float(r["total_sigma_pred"]), 2)
        edge = lean = p_over = None
        flags = []
        if proj is None:
            flags.append("insufficient_model_training_history")
        if mkt["market_total"] is None:
            flags.append("no_total_line")
        if proj is not None and mkt["market_total"] is not None:
            edge = round(proj - mkt["market_total"], 2)
            lean = "OVER" if edge > 0 else "UNDER" if edge < 0 else "PASS"
            from scipy.stats import norm
            p_over = round(float(1 - norm.cdf((mkt["market_total"] - proj) / sigma)), 4)
        p_cal = None if p_over is None else round(float(apply_shrink(p_over, cal["shrink_factor"])), 4)
        if pd.isna(r.get("home_qb_id")) or pd.isna(r.get("away_qb_id")):
            flags.append("starting_qb_unconfirmed")
        records.append({
            "game_id": r["game_id"], "season": int(r["season"]), "week": int(r["week"]),
            "gameday": str(pd.Timestamp(r["gameday"]).date()), "gametime": r.get("gametime"),
            "away_team": r["away_team"], "home_team": r["home_team"], "roof": r.get("roof"),
            "home_qb_id": r.get("home_qb_id"), "away_qb_id": r.get("away_qb_id"),
            **mkt,
            "projected_total": proj, "sigma": sigma, "model_over_prob": p_over, "edge": edge, "lean": lean,
            "raw_model_probability": p_over, "calibrated_probability": p_cal,
            "recommended_units": 0.0,
            "features": {c: (None if pd.isna(r[c]) else round(float(r[c]), 4)) for c in FEATURE_COLS},
            "data_quality_flags": flags,
        })
    generated_at = datetime.now(timezone.utc)
    snapshot = {"generated_at": generated_at.isoformat(), "season": season, "week": week,
                "model_version": MODEL_VERSION, "market": "totals_only", "is_real_data": True,
                "note": "Model lean only. recommended_units is 0 until the Phase 4 decision layer exists. "
                        "Phase 2A baseline: no demonstrated edge for this model.",
                "probability_calibration": snapshot_metadata(cal),
                "display_probability_field": "calibrated_probability",
                "games": records}
    pred_dir.mkdir(parents=True, exist_ok=True)
    out = pred_dir / f"totals_{season}_wk{week:02d}_{generated_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    _write_new(out, json.dumps(snapshot, indent=2))
    _write_new(out.with_suffix(".md"), render_summary(snapshot))
    idx_path = pred_dir / "totals_index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    idx.setdefault(f"{season}_wk{week:02d}", []).append(out.name)
    idx_path.write_text(json.dumps(idx, indent=2))
    logger.info("Wrote immutable totals snapshot %s (%d games)", out, len(records))
    return out
