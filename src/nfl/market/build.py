"""Rebuild every Phase 4 derived table from the immutable raw snapshots (spec section 4, D1-D4).

Outputs go to data/derived/phase4/ (git-ignored; never committed to records). Raw files are the
source of truth, so these tables can be regenerated at any time and always agree with raw.

Holdout seal: this module only accepts 2026+ games and raises if any 2023-2025 game id appears.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.nfl.data.odds_snapshot import ODDS_DIR, quotes_from_raw, spread_quotes_from_raw
from src.nfl.data.weather_forecast import WEATHER_DIR, load_forecasts
from src.nfl.market import phase4_config as C
from src.nfl.market.clv import build_clv_events, edge_bucket, summarize_clv
from src.nfl.market.line_shopping import build_line_shopping, shopping_frequencies
from src.nfl.market.movement import apply_game_kickoff, build_movement
from src.nfl.market.spread_tables import build_spread_line_shopping, build_spread_movement
from src.nfl.market.timing import parse_utc

OUT_DIR = Path("data/derived/phase4")
PRED_DIR = Path("data/predictions")


def assert_forward_only(game_ids) -> None:
    for gid in pd.Series(list(game_ids)).dropna().astype(str):
        season = int(gid[:4]) if gid[:4].isdigit() else None
        if season is not None and season in C.HOLDOUT_SEASONS:
            raise AssertionError(f"holdout season game in Phase 4 data: {gid}")
        if season is not None and season < C.FORWARD_SEASON_MIN:
            raise AssertionError(f"pre-2026 game in Phase 4 data: {gid}")


def forward_schedules(schedules: pd.DataFrame) -> pd.DataFrame:
    """Phase 4 never needs anything before 2026; drop it before any use."""
    return schedules[schedules.season >= C.FORWARD_SEASON_MIN].copy()


def _prediction_index(pred_dir: Path) -> pd.DataFrame:
    rows = []
    for p in sorted(pred_dir.glob("totals_*.json")):
        if p.name == "totals_index.json":
            continue
        snap = json.loads(p.read_text())
        for g in snap.get("games", []):
            rows.append({"prediction_file": p.name, "generated_at": parse_utc(snap["generated_at"]),
                         "model_version": snap.get("model_version"), "game_id": g["game_id"],
                         "market_source": g.get("market_source"), "projected_total": g.get("projected_total"),
                         "model_edge": g.get("edge"), "lean": g.get("lean"),
                         "raw_model_probability": g.get("raw_model_probability", g.get("model_over_prob")),
                         "calibrated_probability": g.get("calibrated_probability")})
    return pd.DataFrame(rows)


def model_fields_for(preds: pd.DataFrame, game_id: str, snapshot_id: str, snapshot_dt) -> dict:
    """The prediction built FROM this snapshot's market if one exists, else the latest prediction
    generated before the snapshot. Never a prediction generated from a later snapshot."""
    if preds.empty:
        return {}
    g = preds[preds.game_id == game_id]
    own = g[g.market_source == f"odds_snapshot:{snapshot_id}"]
    pick = own if len(own) else g[g.generated_at < snapshot_dt]
    if pick.empty:
        return {}
    r = pick.sort_values("generated_at").iloc[-1]
    return {"prediction_file": r.prediction_file, "model_version": r.model_version, "projected_total": r.projected_total,
            "model_edge": r.model_edge, "edge_bucket": edge_bucket(r.model_edge), "lean": r.lean,
            "raw_model_probability": r.raw_model_probability, "calibrated_probability": r.calibrated_probability}


def build_all(schedules: pd.DataFrame, odds_dir: Path = ODDS_DIR, weather_dir: Path = WEATHER_DIR,
              pred_dir: Path = PRED_DIR, out_dir: Path = OUT_DIR) -> dict:
    sched = forward_schedules(schedules)
    quotes = apply_game_kickoff(quotes_from_raw(odds_dir, sched), sched)
    assert_forward_only(quotes.game_id)
    ls = build_line_shopping(quotes)
    finals = {r.game_id: float(r.home_score + r.away_score) for r in sched.itertuples()
              if pd.notna(r.home_score) and pd.notna(r.away_score)}
    mv = build_movement(ls, quotes, finals) if len(ls) else pd.DataFrame()
    ev = build_clv_events(quotes)
    wx = load_forecasts(weather_dir)
    preds = _prediction_index(pred_dir)

    # breakdown keys on CLV events: weather at S (same snapshot only), model edge bucket, overall move direction
    if len(ev):
        wkey = {(r.snapshot_id, r.game_id): r for r in wx.itertuples()} if len(wx) else {}
        mdir = dict(zip(mv.game_key, mv.move_direction)) if len(mv) else {}
        ev["weather_flag_w15"] = [getattr(wkey.get((a, g)), "flag_w15", None) for a, g in zip(ev.action_snapshot_id, ev.game_id)]
        ev["weather_flag_wet"] = [getattr(wkey.get((a, g)), "flag_wet", None) for a, g in zip(ev.action_snapshot_id, ev.game_id)]
        ev["weather_roof_type"] = [getattr(wkey.get((a, g)), "roof_type", None) for a, g in zip(ev.action_snapshot_id, ev.game_id)]
        ev["move_direction"] = ev.game_key.map(mdir)
        ev["edge_bucket"] = [model_fields_for(preds, g, a, parse_utc(t)).get("edge_bucket")
                             for g, a, t in zip(ev.game_id, ev.action_snapshot_id, ev.action_ts)]

    # D4 forward dataset: one row per (game, snapshot)
    fwd = []
    mvi = mv.set_index("game_key") if len(mv) else None
    for r in ls.itertuples() if len(ls) else []:
        sdt = parse_utc(r.snapshot_ts_utc)
        w = wx[(wx.snapshot_id == r.snapshot_id) & (wx.game_id == r.game_id)] if len(wx) else wx
        m = mvi.loc[r.game_key] if mvi is not None and r.game_key in mvi.index else None
        row = {"game_id": r.game_id, "season": int(str(r.game_id)[:4]) if isinstance(r.game_id, str) else None,
               "week": int(str(r.game_id)[5:7]) if isinstance(r.game_id, str) else None,
               "home_team": r.home_team, "away_team": r.away_team, "kickoff_utc": r.kickoff_utc_resolved,
               "snapshot_id": r.snapshot_id, "snapshot_ts_utc": r.snapshot_ts_utc, "snapshot_kind": r.snapshot_kind,
               "minutes_to_kickoff": r.minutes_to_kickoff,
               "market_median": r.all_median_total, "canonical_total": r.all_canonical_total, "dispersion": r.all_line_range,
               "n_books_all": r.all_n_books, "n_books_ny": getattr(r, "ny_n_books", 0),
               "best_over_all": r.all_best_over_total, "best_under_all": r.all_best_under_total,
               "best_over_ny": getattr(r, "ny_best_over_total", None), "best_under_ny": getattr(r, "ny_best_under_total", None),
               "ref_total": getattr(r, "ref_total", None),
               "first_observed_line": None if m is None else m.first_observed_canonical,
               "last_observed_pregame_line": None if m is None else m.last_observed_pregame_canonical,
               "close_quality": None if m is None else m.close_quality,
               "actual_total": finals.get(r.game_id)}
        row.update(model_fields_for(preds, r.game_id, r.snapshot_id, sdt))
        if len(w):
            wr = w.iloc[0]
            row.update({"wx_roof_type": wr.roof_type, "wx_wind_mph_window_mean": wr.wind_mph_window_mean,
                        "wx_precip_prob_window_max": wr.precip_prob_window_max, "wx_flag_w15": wr.flag_w15,
                        "wx_flag_wet": wr.flag_wet, "wx_data_status": wr.data_status})
        if len(ev):
            for side in ("OVER", "UNDER"):
                e = ev[(ev.action_snapshot_id == r.snapshot_id) & (ev.game_key == r.game_key) & (ev.side == side)
                       & (ev.variant == "best_available_ny")]
                row[f"line_clv_{side.lower()}_ny_best"] = e.line_clv.iloc[0] if len(e) else None
                row[f"price_clv_{side.lower()}_ny_best_same_line"] = e.price_clv_same_line_consensus.iloc[0] if len(e) else None
        fwd.append(row)
    fwd = pd.DataFrame(fwd)

    # Phase 5A spreads (research only): same kickoff policy, same holdout seal
    squotes = apply_game_kickoff(spread_quotes_from_raw(odds_dir, sched), sched)
    assert_forward_only(squotes.game_id)
    sls = build_spread_line_shopping(squotes)
    margins = {r.game_id: float(r.home_score - r.away_score) for r in sched.itertuples()
               if pd.notna(r.home_score) and pd.notna(r.away_score)}
    smv = build_spread_movement(sls, margins)

    out_dir.mkdir(parents=True, exist_ok=True)
    tables = {"quotes_rebuilt": quotes, "line_shopping": ls, "movement": mv, "clv_events": ev, "forward_dataset": fwd,
              "spread_quotes_rebuilt": squotes, "spread_line_shopping": sls, "spread_movement": smv}
    for name, df in tables.items():
        df.to_csv(out_dir / f"{name}.csv", index=False)
    summary = {"spec_version": C.SPEC_VERSION,
               "snapshots": int(quotes.snapshot_id.nunique()) if len(quotes) else 0,
               "games": int(quotes.game_key.nunique()) if len(quotes) else 0,
               "shopping_ny_actionable": shopping_frequencies(ls, "ny_") if len(ls) else {},
               "shopping_all_observed": shopping_frequencies(ls, "all_") if len(ls) else {},
               "reference_rows": int(ls.ref_book.notna().sum()) if len(ls) and "ref_book" in ls else 0,
               "spread_snapshots": int(squotes.snapshot_id.nunique()) if len(squotes) else 0,
               "spread_games": int(squotes.game_key.nunique()) if len(squotes) else 0}
    if len(ev):
        summarize_clv(ev).to_csv(out_dir / "clv_summary.csv", index=False)
        summarize_clv(ev, ["action_kind"]).to_csv(out_dir / "clv_summary_by_timing.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary
