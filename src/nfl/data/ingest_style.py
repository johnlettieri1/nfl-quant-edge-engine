"""Phase 3A: extended play-by-play cache for form, pace, PROE, possession and
explosive-play features.

Kept separate from ingest.py on purpose: the Phase 2A/2B baseline reads
data/raw/pbp_epa.parquet and must never change. This module writes its own
per-season files with the extra columns the Phase 3A features need:

  data/raw/pbp_style/pbp_style_<season>.parquet   (+ .meta.json sidecar)
  data/raw/pbp_style.parquet                      concatenation, what the harness reads

Same freeze rules as ingest.py: a completed season is never re-downloaded.
Nothing is fabricated: a failed pull raises.

Holdout discipline: the Phase 3A harness calls this with last_season=2022.
`python run.py data-style` defaults to 1999-2022 for the same reason. Pulling
2023+ requires an explicit --last-season and is only for the forward/live path.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from src.nfl.data.ingest import RAW_DIR, _age_hours, _now, _read_meta, _season_is_frozen

logger = logging.getLogger(__name__)

STYLE_DIR_NAME = "pbp_style"
STYLE_COMBINED = "pbp_style.parquet"

STYLE_PBP_COLUMNS = [
    "game_id", "play_id", "season", "week", "season_type", "home_team", "away_team",
    "posteam", "defteam", "play", "pass", "rush", "epa", "success", "yards_gained",
    "qb_dropback", "qb_kneel", "qb_spike", "qb_scramble", "sack", "pass_attempt",
    "down", "ydstogo", "yardline_100", "qtr", "game_seconds_remaining", "half_seconds_remaining",
    "score_differential", "wp", "xpass", "pass_oe", "no_huddle",
    "fixed_drive", "drive_time_of_possession", "drive_play_count", "fixed_drive_result",
]


def fetch_style_season(season: int, cache_dir: Path = RAW_DIR, force: bool = False,
                       max_age_hours: float = 6.0, schedules: pd.DataFrame | None = None) -> Path:
    d = cache_dir / STYLE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"pbp_style_{season}.parquet"
    meta_path = d / f"pbp_style_{season}.meta.json"
    meta = _read_meta(meta_path)
    if path.exists() and not force:
        if _season_is_frozen(season, schedules) and meta and meta.get("frozen"):
            return path
        if _age_hours(meta) < max_age_hours:
            return path
    import nflreadpy as nfl

    df = nfl.load_pbp([season]).to_pandas()
    if df.empty:
        raise RuntimeError(f"nflreadpy returned empty play-by-play for season {season}")
    keep = [c for c in STYLE_PBP_COLUMNS if c in df.columns]
    df = df[keep].copy()
    for c in STYLE_PBP_COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    df.to_parquet(path, index=False)
    meta_path.write_text(json.dumps({"source": "nflreadpy (nflverse)", "fetched_at": _now(), "seasons": [season],
                                     "rows": int(len(df)), "is_real_data": True,
                                     "frozen": _season_is_frozen(season, schedules), "columns": STYLE_PBP_COLUMNS}, indent=2))
    logger.info("Cached style pbp season=%d rows=%d", season, len(df))
    return path


def fetch_style_pbp(first_season: int = 1999, last_season: int = 2022, cache_dir: Path = RAW_DIR,
                    force: bool = False) -> pd.DataFrame:
    schedules = None
    sp = cache_dir / "schedules.parquet"
    if sp.exists():
        schedules = pd.read_parquet(sp, columns=["season", "home_score"])
    seasons = list(range(first_season, last_season + 1))
    paths = [fetch_style_season(s, cache_dir, force=force, schedules=schedules) for s in seasons]
    df = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    df.to_parquet(cache_dir / STYLE_COMBINED, index=False)
    (cache_dir / "pbp_style.meta.json").write_text(json.dumps(
        {"source": "nflreadpy (nflverse)", "fetched_at": _now(), "seasons": seasons, "rows": int(len(df))}, indent=2))
    return df


def load_cached_style_pbp(cache_dir: Path = RAW_DIR) -> pd.DataFrame:
    path = cache_dir / STYLE_COMBINED
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run `python run.py data-style` first")
    return pd.read_parquet(path)
