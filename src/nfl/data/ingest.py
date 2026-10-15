"""Data ingestion from nflverse via nflreadpy, with an incremental per-season cache.

Phase 2B (Oct 2026): replaces the deprecated/archived nfl_data_py (pinned pandas<2,
Python 3.11 only) with nflreadpy, the official nflverse successor. Public function
names are unchanged so every backtest script, test, and the Phase 2A harness keep
working: fetch_schedules, fetch_pbp_for_epa, load_cached_schedules,
load_cached_pbp_for_epa.

Cache design (data/raw/):
  schedules.parquet              one file, always refreshed on `fetch` (small, carries
                                 lines/juice/QB ids that change during the week)
  pbp/pbp_<season>.parquet       one file per season, EPA columns only (EPA_PBP_COLUMNS)
  pbp/pbp_<season>.meta.json     provenance sidecar: source, fetched_at, rows, is_real_data
  pbp_epa.parquet                concatenation of the per-season files (what the
                                 backtests read); rebuilt only when a season file changed

A season is considered FROZEN once its last game has been played (season < current
season, or all scheduled games have scores). Frozen seasons are never re-downloaded.
The current season is refreshed when its cache is older than `max_age_hours`
(default 6) or when `force=True`. Nothing is ever fabricated: a failed pull raises.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

RAW_DIR = Path("data/raw")
PBP_DIR = RAW_DIR / "pbp"
SOURCE = "nflreadpy (nflverse)"

EPA_PBP_COLUMNS = [
    "game_id", "week", "season_type", "posteam", "defteam",
    "play_type", "epa", "success", "pass", "rush", "down", "wind", "temp",
    "roof", "home_team", "away_team", "play", "wp",
    "passer_id", "qb_dropback", "qb_epa",
    # player-level columns kept so the (out-of-weekly-path) props code still runs
    "complete_pass", "yards_gained", "rusher_id", "rusher",
    "receiver_id", "receiver", "pass_touchdown", "rush_touchdown",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_meta(path: Path, seasons: list[int], rows: int, extra: dict | None = None) -> None:
    meta = {"source": SOURCE, "fetched_at": _now(), "seasons": seasons, "rows": rows, "is_real_data": True}
    if extra:
        meta.update(extra)
    path.write_text(json.dumps(meta, indent=2))


def _read_meta(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _age_hours(meta: dict | None) -> float:
    if not meta or "fetched_at" not in meta:
        return float("inf")
    fetched = datetime.fromisoformat(meta["fetched_at"])
    return (datetime.now(timezone.utc) - fetched).total_seconds() / 3600.0


def current_season(today: datetime | None = None) -> int:
    """NFL season label for a date: September-December -> that year; January-August -> prior year."""
    d = today or datetime.now(timezone.utc)
    return d.year if d.month >= 9 else d.year - 1


# --------------------------------------------------------------------------- schedules

def fetch_schedules(seasons: list[int] | None = None, cache_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Schedules + closing lines/juice + starting QB ids for every season, from nflverse.
    Always refreshes (the file is small and the current week's rows change daily)."""
    import nflreadpy as nfl  # imported lazily so tests that only read the cache do not need network

    df = nfl.load_schedules(seasons if seasons else True).to_pandas()
    if df.empty:
        raise RuntimeError(f"nflreadpy returned empty schedules for seasons={seasons}")
    df = df.sort_values(["season", "week", "gameday", "game_id"]).reset_index(drop=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_dir / "schedules.parquet", index=False)
    _write_meta(cache_dir / "schedules.meta.json", sorted(df.season.unique().tolist()), len(df))
    logger.info("Wrote schedules rows=%d seasons=%d-%d", len(df), df.season.min(), df.season.max())
    return df


def load_cached_schedules(cache_dir: Path = RAW_DIR) -> pd.DataFrame:
    path = cache_dir / "schedules.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run `python run.py data` first")
    return pd.read_parquet(path)


# --------------------------------------------------------------------------- play-by-play

def _season_is_frozen(season: int, schedules: pd.DataFrame | None) -> bool:
    if season < current_season():
        return True
    if schedules is None:
        return False
    s = schedules[schedules.season == season]
    return bool(len(s) > 0 and s["home_score"].notna().all())


def fetch_pbp_season(season: int, cache_dir: Path = RAW_DIR, force: bool = False,
                     max_age_hours: float = 6.0, schedules: pd.DataFrame | None = None) -> Path:
    """Download one season of play-by-play (EPA columns only) into the per-season cache.
    Frozen seasons are skipped if present; the current season is refreshed when stale.
    Returns the cache path."""
    pbp_dir = cache_dir / "pbp"
    pbp_dir.mkdir(parents=True, exist_ok=True)
    path = pbp_dir / f"pbp_{season}.parquet"
    meta_path = pbp_dir / f"pbp_{season}.meta.json"
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
    keep = [c for c in EPA_PBP_COLUMNS if c in df.columns]
    missing = [c for c in EPA_PBP_COLUMNS if c not in df.columns]
    df = df[keep].copy()
    for c in missing:
        df[c] = pd.NA
    df.to_parquet(path, index=False)
    _write_meta(meta_path, [season], len(df), {"frozen": _season_is_frozen(season, schedules), "columns": EPA_PBP_COLUMNS})
    logger.info("Cached pbp season=%d rows=%d frozen=%s", season, len(df), _season_is_frozen(season, schedules))
    return path


def fetch_pbp_for_epa(seasons: list[int], cache_dir: Path = RAW_DIR, force: bool = False,
                      max_age_hours: float = 6.0) -> pd.DataFrame:
    """Incremental: pulls only seasons that are missing or stale, then rebuilds the
    combined pbp_epa.parquet that the backtests read."""
    schedules = None
    sched_path = cache_dir / "schedules.parquet"
    if sched_path.exists():
        schedules = pd.read_parquet(sched_path, columns=["season", "home_score"])
    paths = [fetch_pbp_season(s, cache_dir, force=force, max_age_hours=max_age_hours, schedules=schedules) for s in seasons]
    combined_path = cache_dir / "pbp_epa.parquet"
    combined_meta = _read_meta(cache_dir / "pbp_epa.meta.json")
    newest_part = max(p.stat().st_mtime for p in paths)
    if combined_path.exists() and combined_meta and combined_meta.get("seasons") == sorted(seasons) \
            and combined_path.stat().st_mtime >= newest_part:
        return pd.read_parquet(combined_path)
    df = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    df.to_parquet(combined_path, index=False)
    _write_meta(cache_dir / "pbp_epa.meta.json", sorted(seasons), len(df))
    logger.info("Rebuilt pbp_epa.parquet rows=%d seasons=%d", len(df), len(seasons))
    return df


def load_cached_pbp_for_epa(cache_dir: Path = RAW_DIR) -> pd.DataFrame:
    path = cache_dir / "pbp_epa.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run `python run.py data` first")
    return pd.read_parquet(path)


def refresh_all(first_season: int = 1999, last_season: int | None = None, cache_dir: Path = RAW_DIR,
                force: bool = False) -> dict:
    """What `python run.py data` calls: refresh schedules, then incrementally refresh pbp."""
    last = last_season or current_season()
    seasons = list(range(first_season, last + 1))
    sched = fetch_schedules(cache_dir=cache_dir)
    pbp = fetch_pbp_for_epa(seasons, cache_dir=cache_dir, force=force)
    return {"schedules_rows": int(len(sched)), "pbp_rows": int(len(pbp)), "seasons": [first_season, last]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(refresh_all())
