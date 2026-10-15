"""Phase 3A features: Bayesian-prior exponentially weighted form, neutral pace,
pass rate over expectation, explosive-play environment, expected possessions.

Definitions are frozen in docs/phase3a/PHASE_3A_FEATURE_SPEC.md and
src/nfl/features/phase3a_config.py. Two stages, both leak-free:

  1. aggregate_style_to_game_team(): one row per (game_id, team) with every
     per-game metric. Looks only *within* a single game.
  2. add_phase3a_features(): one chronological pass over the schedule. For each
     game, the pre-game EW estimate is attached first; only then is that game's
     own value folded into the team's running sums. A game can therefore never
     see itself or any later game.

Team identity: nflverse play-by-play uses current franchise codes for every
season (LA, LAC, LV) while schedules use the code of the day (STL, SD, OAK).
Both sides are normalized to the current franchise code here (FRANCHISE map),
so a team's history is continuous across relocations and play-by-play rows
match schedule rows. (The frozen ridge8_v1 baseline does not do this; see the
Phase 3A report.)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.nfl.features import phase3a_config as C

FRANCHISE = {"STL": "LA", "SD": "LAC", "OAK": "LV"}

TEAM_METRICS = [
    "off_pts", "def_pts", "off_epa", "def_epa", "off_pass_epa", "off_rush_epa", "off_sr", "def_sr",
    "spp", "off_plays", "def_plays", "nohuddle", "drives", "plays_per_drive", "neutral_pass", "proe",
    "def_proe_faced", "xpl_pass", "xpl_rush", "xpl_pass_allowed", "xpl_rush_allowed",
    "drive_secs", "drive_secs_faced",
]


def normalize_team(code):
    return FRANCHISE.get(code, code)


def normalize_schedule_teams(games: pd.DataFrame) -> pd.DataFrame:
    g = games.copy()
    g["home_team"] = g["home_team"].map(normalize_team)
    g["away_team"] = g["away_team"].map(normalize_team)
    return g


def _mmss_to_seconds(s: pd.Series) -> pd.Series:
    parts = s.astype("string").str.split(":", n=1, expand=True)
    if parts.shape[1] < 2:
        return pd.Series(np.nan, index=s.index)
    return pd.to_numeric(parts[0], errors="coerce") * 60 + pd.to_numeric(parts[1], errors="coerce")


# --------------------------------------------------------------------------- stage 1: within-game

def aggregate_style_to_game_team(pbp: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """One row per (game_id, team). `games` supplies points and season; team codes
    in both inputs are normalized to franchise codes."""
    p = pbp.copy()
    for c in ("posteam", "defteam"):
        p[c] = p[c].map(normalize_team)
    p = p[p["posteam"].notna() & (p["posteam"] != "") & p["defteam"].notna() & (p["defteam"] != "")]
    if "season" not in p.columns or p["season"].isna().any():
        p["season"] = p["game_id"].str[:4].astype(int)

    kneel = p["qb_kneel"].fillna(0) == 1
    spike = p["qb_spike"].fillna(0) == 1
    scrim_any = (p["play"] == 1) & ((p["pass"] == 1) | (p["rush"] == 1))
    P = scrim_any & p["epa"].notna() & ~kneel & ~spike
    drop = P & (p["qb_dropback"] == 1)
    run = P & (p["rush"] == 1) & (p["qb_dropback"].fillna(0) == 0)
    neutral = (P & p["down"].isin(C.NEUTRAL_DOWNS) & (p["qtr"] <= C.NEUTRAL_MAX_QTR)
               & (p["half_seconds_remaining"] > C.NEUTRAL_MIN_HALF_SECONDS)
               & (p["wp"] >= C.NEUTRAL_WP_LOW) & (p["wp"] <= C.NEUTRAL_WP_HIGH))

    keys = ["game_id", "posteam"]
    pP = p[P]
    off = pP.groupby(keys).agg(off_epa=("epa", "mean"), off_sr=("success", "mean")).reset_index()
    dfn = pP.groupby(["game_id", "defteam"]).agg(def_epa=("epa", "mean"), def_sr=("success", "mean")).reset_index()

    pd_ = p[drop].assign(xpl=lambda d: (d["yards_gained"] >= C.EXPLOSIVE_PASS_YARDS).astype(float))
    pr_ = p[run].assign(xpl=lambda d: (d["yards_gained"] >= C.EXPLOSIVE_RUSH_YARDS).astype(float))
    off_pass = pd_.groupby(keys).agg(off_pass_epa=("epa", "mean"), xpl_pass=("xpl", "mean")).reset_index()
    off_run = pr_.groupby(keys).agg(off_rush_epa=("epa", "mean"), xpl_rush=("xpl", "mean")).reset_index()
    def_pass = pd_.groupby(["game_id", "defteam"]).agg(xpl_pass_allowed=("xpl", "mean")).reset_index()
    def_run = pr_.groupby(["game_id", "defteam"]).agg(xpl_rush_allowed=("xpl", "mean")).reset_index()

    # plays (all scrimmage incl. kneels/spikes), no-huddle, drives
    sc = p[scrim_any]
    plays = sc.groupby(keys).size().rename("off_plays").reset_index()
    dplays = sc.groupby(["game_id", "defteam"]).size().rename("def_plays").reset_index()
    nh = pP.groupby(keys)["no_huddle"].mean().rename("nohuddle").reset_index()
    dr = pP.dropna(subset=["fixed_drive"]).groupby(keys)["fixed_drive"].nunique().rename("drives").reset_index()

    # drive durations (one value per drive that has >= 1 P play)
    drv = pP.dropna(subset=["fixed_drive"]).drop_duplicates(["game_id", "posteam", "fixed_drive"])[
        ["game_id", "posteam", "defteam", "fixed_drive", "drive_time_of_possession"]].copy()
    drv["secs"] = _mmss_to_seconds(drv["drive_time_of_possession"])
    dsecs = drv.groupby(keys)["secs"].mean().rename("drive_secs").reset_index()
    dsecs_f = drv.groupby(["game_id", "defteam"])["secs"].mean().rename("drive_secs_faced").reset_index()

    # neutral pass rate and PROE
    pn = p[neutral]
    npass = pn.groupby(keys)["qb_dropback"].mean().rename("neutral_pass").reset_index()
    pnx = pn[pn["xpass"].notna()].assign(oe=lambda d: d["qb_dropback"] - d["xpass"])
    proe = pnx.groupby(keys)["oe"].mean().rename("proe").reset_index()
    dproe = pnx.groupby(["game_id", "defteam"])["oe"].mean().rename("def_proe_faced").reset_index()

    # neutral seconds per play: gap from the previous P play of the same offense in the same drive
    q = pP[["game_id", "posteam", "fixed_drive", "game_seconds_remaining", "play_id"]].copy()
    q["is_neutral"] = neutral[P].to_numpy()
    q = q.sort_values(["game_id", "posteam", "fixed_drive", "game_seconds_remaining", "play_id"],
                      ascending=[True, True, True, False, True])
    prev_gsr = q.groupby(["game_id", "posteam", "fixed_drive"])["game_seconds_remaining"].shift(1)
    q["gap"] = prev_gsr - q["game_seconds_remaining"]
    ok = q["is_neutral"] & (q["gap"] > C.SPP_GAP_MIN_EXCLUSIVE) & (q["gap"] <= C.SPP_GAP_MAX_INCLUSIVE)
    spp = q[ok].groupby(keys)["gap"].agg(["mean", "size"]).reset_index()
    spp = spp[spp["size"] >= C.SPP_MIN_GAPS].rename(columns={"mean": "spp"})[keys + ["spp"]]

    def _d(df):  # defteam-keyed -> team
        return df.rename(columns={"defteam": "team"})

    def _o(df):
        return df.rename(columns={"posteam": "team"})

    gt = _o(off)
    for part in (_d(dfn), _o(off_pass), _o(off_run), _d(def_pass), _d(def_run), _o(plays), _d(dplays), _o(nh),
                 _o(dr), _o(dsecs), _d(dsecs_f), _o(npass), _o(proe), _d(dproe), _o(spp)):
        gt = gt.merge(part, on=["game_id", "team"], how="outer")
    gt["plays_per_drive"] = gt["off_plays"] / gt["drives"]

    # points from the schedule
    sched = normalize_schedule_teams(games)[["game_id", "season", "home_team", "away_team", "home_score", "away_score"]]
    h = sched.rename(columns={"home_team": "team", "home_score": "off_pts", "away_score": "def_pts"})[
        ["game_id", "season", "team", "off_pts", "def_pts"]]
    a = sched.rename(columns={"away_team": "team", "away_score": "off_pts", "home_score": "def_pts"})[
        ["game_id", "season", "team", "off_pts", "def_pts"]]
    pts = pd.concat([h, a], ignore_index=True)
    gt = pts.merge(gt, on=["game_id", "team"], how="left")

    # reliability: treat unreliable seasons as missing
    for m, first in C.RELIABLE_FROM.items():
        gt.loc[gt["season"] < first, m] = np.nan
    return gt[["game_id", "season", "team"] + TEAM_METRICS]


# --------------------------------------------------------------------------- stage 2: chronological EW

def season_priors(game_team: pd.DataFrame) -> dict[int, dict[str, float]]:
    """prior_m(S) = league mean over season S-1 team-games; default if S-1 unobserved."""
    means = game_team.groupby("season")[TEAM_METRICS].mean()
    out = {}
    seasons = sorted(game_team["season"].unique())
    for s in range(min(seasons), max(seasons) + 2):
        prev = means.loc[s - 1] if (s - 1) in means.index else None
        out[s] = {m: (float(prev[m]) if prev is not None and pd.notna(prev[m]) else C.DEFAULTS[m]) for m in TEAM_METRICS}
    return out


def add_phase3a_features(games: pd.DataFrame, game_team: pd.DataFrame, half_life: float = C.HALF_LIFE_PRIMARY,
                         carryover: float = C.OFFSEASON_CARRYOVER, k: float = C.PRIOR_STRENGTH_GAMES) -> pd.DataFrame:
    """Attach pre-game EW team estimates (home_/away_<metric>_ew) and the game-level
    Phase 3A features. `games` must already be franchise-normalized (see normalize_schedule_teams)."""
    g = games.copy()
    g["gameday"] = pd.to_datetime(g["gameday"])
    g = g.sort_values(["season", "week", "gameday", "game_id"]).reset_index(drop=True)
    priors = season_priors(game_team)
    by_game = {gid: grp.set_index("team") for gid, grp in game_team.groupby("game_id")}
    decay = 0.5 ** (1.0 / half_life)
    M = len(TEAM_METRICS)
    sw: dict[str, np.ndarray] = {}
    swx: dict[str, np.ndarray] = {}
    last_season: dict[str, int] = {}
    out = {f"{side}_{m}_ew": np.empty(len(g)) for side in ("home", "away") for m in TEAM_METRICS}

    for i, row in enumerate(g.itertuples(index=False)):
        season = int(row.season)
        pr = np.array([priors.get(season, priors[max(priors)])[m] for m in TEAM_METRICS])
        for team, side in ((row.home_team, "home"), (row.away_team, "away")):
            if team not in sw:
                sw[team] = np.zeros(M); swx[team] = np.zeros(M); last_season[team] = season
            if season > last_season[team]:
                f = carryover ** (season - last_season[team])
                sw[team] *= f; swx[team] *= f; last_season[team] = season
            est = (k * pr + swx[team]) / (k + sw[team])
            for j, m in enumerate(TEAM_METRICS):
                out[f"{side}_{m}_ew"][i] = est[j]
        # only after both estimates are attached does this game enter the history
        rows = by_game.get(row.game_id)
        if rows is None:
            continue
        for team in (row.home_team, row.away_team):
            if team not in rows.index:
                continue
            x = rows.loc[team, TEAM_METRICS].to_numpy(dtype=float)
            sw[team] *= decay; swx[team] *= decay
            obs = ~np.isnan(x)
            sw[team][obs] += 1.0
            swx[team][obs] += x[obs]

    for c, v in out.items():
        g[c] = v
    return add_game_level_features(g, priors)


def add_game_level_features(g: pd.DataFrame, priors: dict[int, dict[str, float]]) -> pd.DataFrame:
    H = lambda m: g[f"home_{m}_ew"]  # noqa: E731
    A = lambda m: g[f"away_{m}_ew"]  # noqa: E731
    # Group A
    g["form_pts_home"] = H("off_pts") + A("def_pts")
    g["form_pts_away"] = A("off_pts") + H("def_pts")
    g["form_epa_home"] = H("off_epa") + A("def_epa")
    g["form_epa_away"] = A("off_epa") + H("def_epa")
    g["form_pass_epa_sum"] = H("off_pass_epa") + A("off_pass_epa")
    g["form_rush_epa_sum"] = H("off_rush_epa") + A("off_rush_epa")
    g["form_sr_home"] = H("off_sr") + A("def_sr")
    g["form_sr_away"] = A("off_sr") + H("def_sr")
    # Group B
    g["pace_spp_sum"] = H("spp") + A("spp")
    g["pace_off_plays_sum"] = H("off_plays") + A("off_plays")
    g["pace_def_plays_sum"] = H("def_plays") + A("def_plays")
    g["pace_nohuddle_sum"] = H("nohuddle") + A("nohuddle")
    g["pace_drives_sum"] = H("drives") + A("drives")
    g["pace_ppd_sum"] = H("plays_per_drive") + A("plays_per_drive")
    g["proe_sum"] = H("proe") + A("proe")
    g["neutral_pass_sum"] = H("neutral_pass") + A("neutral_pass")
    g["def_proe_faced_sum"] = H("def_proe_faced") + A("def_proe_faced")
    # Group C
    g["xpl_pass_home"] = H("xpl_pass") + A("xpl_pass_allowed")
    g["xpl_pass_away"] = A("xpl_pass") + H("xpl_pass_allowed")
    g["xpl_rush_home"] = H("xpl_rush") + A("xpl_rush_allowed")
    g["xpl_rush_away"] = A("xpl_rush") + H("xpl_rush_allowed")
    # Group D
    league_ds = g["season"].map(lambda s: priors.get(int(s), priors[max(priors)])["drive_secs"])
    t_h = H("drive_secs") * A("drive_secs_faced") / league_ds
    t_a = A("drive_secs") * H("drive_secs_faced") / league_ds
    g["exp_poss_home"] = C.REGULATION_SECONDS / (t_h + t_a)
    g["exp_poss_away"] = g["exp_poss_home"]
    g["exp_game_possessions"] = g["exp_poss_home"] + g["exp_poss_away"]
    return g
