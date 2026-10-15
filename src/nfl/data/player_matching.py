"""Match The Odds API's player names (free text, e.g. "Patrick Mahomes") to
nflverse's gsis_id (e.g. "00-0033873"), using nflverse's own player
reference table (nfl_data_py.import_players()) as the crosswalk -- never a
fuzzy/approximate guess when a name is ambiguous or unrecognized.
"""
from __future__ import annotations

import re

import nflreadpy as nfl  # Phase 2B: nfl_data_py is archived; nflreadpy is the nflverse successor
import pandas as pd

_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?")
_PUNCT_RE = re.compile(r"[.'\-]")
_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    n = name.lower()
    n = _PUNCT_RE.sub("", n)
    n = _SUFFIX_RE.sub("", n)
    n = _WS_RE.sub(" ", n).strip()
    return n


def build_name_to_gsis_id(active_only: bool = True) -> dict[str, list[str]]:
    """normalized_name -> list of gsis_ids sharing that name (usually one;
    a list surfaces ambiguity honestly rather than silently picking one).
    Restricted to recently-active players by default to cut down on
    retired-player name collisions."""
    players = nfl.load_players().to_pandas()
    if active_only:
        players = players[players["last_season"].fillna(0) >= 2023]
    players = players[players["gsis_id"].notna() & players["display_name"].notna()]

    mapping: dict[str, list[str]] = {}
    for row in players.itertuples(index=False):
        key = normalize_name(row.display_name)
        mapping.setdefault(key, []).append(row.gsis_id)
    return mapping


def match_player_name(name: str, name_to_gsis: dict[str, list[str]]) -> str | None:
    """Returns a gsis_id only for an unambiguous match. None (never a
    guess) if the name isn't recognized or maps to more than one player."""
    candidates = name_to_gsis.get(normalize_name(name))
    if candidates and len(candidates) == 1:
        return candidates[0]
    return None
