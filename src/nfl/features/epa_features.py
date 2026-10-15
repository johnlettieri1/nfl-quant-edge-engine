"""Leak-free trailing EPA (expected points added) features -- the single
most predictive public NFL metric per the project brief, and until now
entirely missing from this system.

Two-stage, both leak-free:
  1. Aggregate play-by-play to one row per (game, team): offensive EPA/play,
     defensive EPA/play allowed, success rate, split by pass/rush. This
     stage only ever looks *within* a single game (no leakage risk).
  2. Roll that game-level table forward chronologically per team, exactly
     like features/rolling_scoring.py: the trailing value attached to a
     game is computed only from that team's games strictly before it.

Source: nflverse play-by-play via nflreadpy.load_pbp (Phase 2B; was nfl_data_py). `play == 1`
is nflverse's own flag for "this row counts as a real play" (excludes
timeouts, penalties-only, spikes counted separately, etc.) -- filtering on
it (not just play_type) is what nflverse's own EPA leaderboards do.

Garbage-time filtering: available (`filter_garbage_time=True`), OFF by
default -- ATTEMPTED AND REVERTED, kept as documentation, same as the
heteroskedastic variance model in models/variance.py.

The idea (a standard public-analytics convention, e.g. rbsdm.com): exclude
plays where the pre-play win probability (`wp`, nflfastR's own model,
populated for ~99.4% of plays across 1999-2026 -- checked directly) is
outside [0.1, 0.9], since a team padding a 35-point lead plays differently
than it does in a competitive game.

Backtested result: it made things WORSE almost across the board -- total
model accuracy gain from EPA was entirely wiped out (0.509 -> 0.506, back to
the no-EPA baseline), spread ECE got worse (0.068 -> 0.070), moneyline
ensemble accuracy dropped (0.6665 -> 0.6648). Plausible reason: this system
already smooths EPA over a 10-game trailing window; cutting ~23% of plays
per game shrinks the effective sample feeding that window, and the added
estimation noise outweighs the bias reduction from excluding blowout snaps.
Reverted to off by default. Plays with missing `wp` are always kept when the
filter is on (never dropped for a reason we can't verify).
"""
from __future__ import annotations

from collections import defaultdict, deque

import numpy as np
import pandas as pd

LEAGUE_AVG_EPA = 0.0  # neutral prior for a team's first `window` games ever
LEAGUE_AVG_SUCCESS = 0.45

GARBAGE_TIME_WP_LOW = 0.1
GARBAGE_TIME_WP_HIGH = 0.9


def aggregate_epa_to_game_team(pbp: pd.DataFrame, filter_garbage_time: bool = False) -> pd.DataFrame:
    """One row per (game_id, team): offensive EPA/play, defensive EPA/play
    allowed, success rate (offense), split pass/rush EPA. Purely within-game
    aggregation -- no chronological ordering assumptions, so no leakage risk
    at this stage."""
    plays = pbp[(pbp["play"] == 1) & pbp["epa"].notna()].copy()
    if filter_garbage_time and "wp" in plays.columns:
        is_garbage = (plays["wp"] < GARBAGE_TIME_WP_LOW) | (plays["wp"] > GARBAGE_TIME_WP_HIGH)
        plays = plays[~is_garbage.fillna(False)]  # unknown wp -> keep the play

    off = plays.groupby(["game_id", "posteam"]).agg(
        off_epa=("epa", "mean"),
        off_success=("success", "mean"),
        off_plays=("epa", "size"),
    ).reset_index().rename(columns={"posteam": "team"})

    pass_plays = plays[plays["pass"] == 1]
    off_pass = pass_plays.groupby(["game_id", "posteam"])["epa"].mean().reset_index(
        name="off_pass_epa").rename(columns={"posteam": "team"})

    rush_plays = plays[plays["rush"] == 1]
    off_rush = rush_plays.groupby(["game_id", "posteam"])["epa"].mean().reset_index(
        name="off_rush_epa").rename(columns={"posteam": "team"})

    defn = plays.groupby(["game_id", "defteam"]).agg(
        def_epa_allowed=("epa", "mean"),
        def_success_allowed=("success", "mean"),
    ).reset_index().rename(columns={"defteam": "team"})

    game_team = off.merge(off_pass, on=["game_id", "team"], how="left") \
                    .merge(off_rush, on=["game_id", "team"], how="left") \
                    .merge(defn, on=["game_id", "team"], how="left")
    return game_team


def _sort_games(games: pd.DataFrame) -> pd.DataFrame:
    g = games.copy()
    g["gameday"] = pd.to_datetime(g["gameday"])
    return g.sort_values(["season", "week", "gameday", "game_id"]).reset_index(drop=True)


def add_trailing_epa_features(
    games: pd.DataFrame, pbp: pd.DataFrame, window: int = 10, filter_garbage_time: bool = False,
) -> pd.DataFrame:
    """Attach trailing (leak-free) EPA features to a games/schedule frame.

    Adds, for both home and away: {off_epa, off_pass_epa, off_rush_epa,
    off_success, def_epa_allowed, def_success_allowed}_trailing.
    """
    game_team = aggregate_epa_to_game_team(pbp, filter_garbage_time=filter_garbage_time)
    by_game = {gid: grp.set_index("team") for gid, grp in game_team.groupby("game_id")}

    g = _sort_games(games)

    metric_cols = ["off_epa", "off_pass_epa", "off_rush_epa", "off_success",
                   "def_epa_allowed", "def_success_allowed"]
    history: dict[str, dict[str, deque]] = defaultdict(
        lambda: {m: deque(maxlen=window) for m in metric_cols}
    )

    out_cols = {f"home_{m}_trailing": [] for m in metric_cols}
    out_cols.update({f"away_{m}_trailing": [] for m in metric_cols})

    defaults = {
        "off_epa": LEAGUE_AVG_EPA, "off_pass_epa": LEAGUE_AVG_EPA, "off_rush_epa": LEAGUE_AVG_EPA,
        "off_success": LEAGUE_AVG_SUCCESS,
        "def_epa_allowed": LEAGUE_AVG_EPA, "def_success_allowed": LEAGUE_AVG_SUCCESS,
    }

    for row in g.itertuples(index=False):
        home, away, gid = row.home_team, row.away_team, row.game_id

        for team, prefix in ((home, "home"), (away, "away")):
            for m in metric_cols:
                dq = history[team][m]
                val = float(np.mean(dq)) if dq else defaults[m]
                out_cols[f"{prefix}_{m}_trailing"].append(val)

        game_rows = by_game.get(gid)
        if game_rows is not None:
            for team in (home, away):
                if team in game_rows.index:
                    for m in metric_cols:
                        v = game_rows.loc[team, m]
                        if pd.notna(v):
                            history[team][m].append(float(v))

    for col, vals in out_cols.items():
        g[col] = vals
    return g
