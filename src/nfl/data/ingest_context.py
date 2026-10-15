"""Phase 3B-3 context inputs: game weather text, injury reports, snap counts, player ids.

Research pulls stop at 2022 (asserted): the 2023-2025 holdout is never downloaded for research.
Cache: data/raw/context/{weather_text,injuries,snap_counts,players}.parquet (+ meta sidecars).
Nothing is fabricated; a failed pull raises.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from src.nfl.data.ingest import RAW_DIR, _now
from src.nfl.models.residual import phase3b_config as C

logger = logging.getLogger(__name__)
CTX = "context"


def _guard(last_season: int, allow_live: bool) -> None:
    if last_season > C.RESEARCH_LAST_SEASON and not allow_live:
        raise ValueError(f"research context pulls stop at {C.RESEARCH_LAST_SEASON}; holdout seasons are not downloaded")


def _save(df: pd.DataFrame, cache_dir: Path, name: str, seasons: list[int]) -> pd.DataFrame:
    d = cache_dir / CTX; d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / f"{name}.parquet", index=False)
    (d / f"{name}.meta.json").write_text(json.dumps({"source": "nflreadpy (nflverse)", "fetched_at": _now(),
                                                     "seasons": seasons, "rows": int(len(df)), "is_real_data": True}, indent=2))
    logger.info("Cached %s rows=%d", name, len(df))
    return df


def fetch_context(first_season: int = C.FIRST_SEASON, last_season: int = C.RESEARCH_LAST_SEASON,
                  cache_dir: Path = RAW_DIR, allow_live: bool = False) -> dict:
    _guard(last_season, allow_live)
    import nflreadpy as nfl
    seasons = list(range(first_season, last_season + 1))
    wx = []
    for s in seasons:
        p = nfl.load_pbp([s]).to_pandas()[["game_id", "weather"]]
        wx.append(p.dropna(subset=["weather"]).groupby("game_id", as_index=False).weather.first())
    wx = _save(pd.concat(wx, ignore_index=True), cache_dir, "weather_text", seasons)
    inj_seasons = [s for s in seasons if s >= 2009]
    inj = _save(nfl.load_injuries(inj_seasons).to_pandas(), cache_dir, "injuries", inj_seasons)
    snap_seasons = [s for s in seasons if s >= C.INJURY_FIRST_SEASON]
    snaps = _save(nfl.load_snap_counts(snap_seasons).to_pandas(), cache_dir, "snap_counts", snap_seasons)
    pl = nfl.load_players().to_pandas()[["gsis_id", "pfr_id", "display_name", "position"]]
    _save(pl, cache_dir, "players", [])
    for name, df in (("injuries", inj), ("snap_counts", snaps)):
        assert df["season"].max() <= last_season, f"{name} contains seasons past {last_season}"
    assert wx.game_id.str[:4].astype(int).max() <= last_season
    return {"weather_games": int(len(wx)), "injury_rows": int(len(inj)), "snap_rows": int(len(snaps)), "players": int(len(pl))}


def load_context(cache_dir: Path = RAW_DIR) -> dict[str, pd.DataFrame]:
    d = cache_dir / CTX
    out = {n: pd.read_parquet(d / f"{n}.parquet") for n in ("weather_text", "injuries", "snap_counts", "players")}
    for n in ("injuries", "snap_counts"):
        out[n] = out[n][out[n].season < C.HOLDOUT_START]
    out["weather_text"] = out["weather_text"][out["weather_text"].game_id.str[:4].astype(int) < C.HOLDOUT_START]
    return out
