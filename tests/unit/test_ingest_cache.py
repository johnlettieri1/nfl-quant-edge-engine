"""Phase 2B: incremental play-by-play cache must never re-download a frozen season,
and must refresh a stale current season. No network: nflreadpy is stubbed."""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.nfl.data import ingest


def _stub_nflreadpy(monkeypatch, calls: list):
    class _PL:
        def __init__(self, df): self._df = df
        def to_pandas(self): return self._df

    def load_pbp(seasons):
        calls.append(("pbp", tuple(seasons)))
        rows = [{"game_id": f"{seasons[0]}_01_A_B", "week": 1, "season_type": "REG", "posteam": "A", "defteam": "B",
                 "play_type": "pass", "epa": 0.1, "success": 1.0, "pass": 1.0, "rush": 0.0, "down": 1, "wind": None, "temp": None,
                 "roof": "dome", "home_team": "B", "away_team": "A", "play": 1.0, "wp": 0.5, "passer_id": "00-1",
                 "qb_dropback": 1.0, "qb_epa": 0.1}]
        return _PL(pd.DataFrame(rows))

    def load_schedules(seasons=True):
        calls.append(("schedules", seasons))
        return _PL(pd.DataFrame([{"game_id": "2015_01_A_B", "season": 2015, "week": 1, "gameday": "2015-09-10",
                                  "home_team": "B", "away_team": "A", "home_score": 20, "away_score": 17}]))

    mod = types.SimpleNamespace(load_pbp=load_pbp, load_schedules=load_schedules)
    monkeypatch.setitem(sys.modules, "nflreadpy", mod)


def test_frozen_season_is_not_redownloaded(tmp_path, monkeypatch):
    calls = []
    _stub_nflreadpy(monkeypatch, calls)
    p1 = ingest.fetch_pbp_season(2015, cache_dir=tmp_path)
    assert p1.exists() and calls == [("pbp", (2015,))]
    meta = json.loads((tmp_path / "pbp" / "pbp_2015.meta.json").read_text())
    assert meta["frozen"] is True and meta["is_real_data"] is True
    ingest.fetch_pbp_season(2015, cache_dir=tmp_path)
    assert calls == [("pbp", (2015,))], "frozen season was re-downloaded"


def test_current_season_refreshes_when_stale(tmp_path, monkeypatch):
    calls = []
    _stub_nflreadpy(monkeypatch, calls)
    season = ingest.current_season()
    ingest.fetch_pbp_season(season, cache_dir=tmp_path)
    assert len(calls) == 1
    ingest.fetch_pbp_season(season, cache_dir=tmp_path, max_age_hours=6)
    assert len(calls) == 1, "fresh current-season cache should be reused"
    meta_path = tmp_path / "pbp" / f"pbp_{season}.meta.json"
    meta = json.loads(meta_path.read_text())
    meta["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    meta_path.write_text(json.dumps(meta))
    ingest.fetch_pbp_season(season, cache_dir=tmp_path, max_age_hours=6)
    assert len(calls) == 2, "stale current-season cache should refresh"


def test_combined_file_rebuilds_only_when_parts_change(tmp_path, monkeypatch):
    calls = []
    _stub_nflreadpy(monkeypatch, calls)
    df = ingest.fetch_pbp_for_epa([2014, 2015], cache_dir=tmp_path)
    assert len(df) == 2 and set(ingest.EPA_PBP_COLUMNS) <= set(df.columns)
    first_mtime = (tmp_path / "pbp_epa.parquet").stat().st_mtime
    ingest.fetch_pbp_for_epa([2014, 2015], cache_dir=tmp_path)
    assert (tmp_path / "pbp_epa.parquet").stat().st_mtime == first_mtime
    assert calls == [("pbp", (2014,)), ("pbp", (2015,))]


def test_current_season_label():
    assert ingest.current_season(datetime(2026, 10, 15, tzinfo=timezone.utc)) == 2026
    assert ingest.current_season(datetime(2027, 2, 1, tzinfo=timezone.utc)) == 2026
