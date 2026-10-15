"""Data-quality fix: relocated-team code aliases (Rams, Chargers, Raiders).

The defect
----------
nflverse play-by-play labels every season with the franchise's *current* code
(LA, LAC, LV), while nflverse schedules use the code *of the day*:

    Rams      STL  1999-2015  ->  LA   2016+
    Chargers  SD   1999-2016  ->  LAC  2017+
    Raiders   OAK  1999-2019  ->  LV   2020+

Any feature that joins play-by-play to schedules by team code (the trailing EPA
features in features/epa_features.py) silently finds no rows for STL, SD and OAK
games, so those teams never accumulate EPA history in those seasons. Code-keyed
rolling windows (features/rolling_scoring.py) also restart at each relocation.

The fix
-------
Normalize schedule codes to the current franchise code before any join or
team-history lookup. Play-by-play already uses current codes, so after this
both sides agree. Current codes pass through unchanged, so the function is
idempotent and safe on any season.

Scope (deliberate)
------------------
This module is used by the Phase 3A research code. It is NOT applied to the
frozen `ridge8_v1` model or the live weekly path, which keep their Phase 2A
behavior exactly (see docs/phase3a/DATA_QUALITY_RELOCATED_TEAMS.md). Wiring it
into a live model is a model-version change and needs its own approval.
"""
from __future__ import annotations

import pandas as pd

RELOCATED_TEAM_ALIASES: dict[str, str] = {"STL": "LA", "SD": "LAC", "OAK": "LV"}


def normalize_team_code(code):
    """Historical alias -> current franchise code; anything else unchanged (including NaN)."""
    return RELOCATED_TEAM_ALIASES.get(code, code)


def normalize_schedule_team_codes(games: pd.DataFrame, cols: tuple[str, ...] = ("home_team", "away_team")) -> pd.DataFrame:
    """Return a copy of `games` with the given team-code columns normalized."""
    g = games.copy()
    for c in cols:
        if c in g.columns:
            g[c] = g[c].map(normalize_team_code)
    return g
