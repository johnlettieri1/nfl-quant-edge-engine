"""Phase 5B spread residual model: predict (margin - closing spread), walk-forward.

Pre-registered in docs/phase5/PHASE_5B_PREREGISTRATION.md (approved Oct 29 2026, frozen).

Target: residual = (home_score - away_score) - spread_line. nflverse spread_line is POSITIVE when the
home team is favored (tests/leakage/test_spread_sign_convention.py). r_hat > 0 picks the home side.

Recipe (fixed, no tuning): Ridge alpha 10, features standardized on the training fold only, expanding
window (fit on every completed game with season < S, at least 500 games), flat per-season sigma from
training residuals. Every feature is "home minus away" and uses only games that finished before kickoff;
the trailing-window builders are the ones already leakage-tested in Phase 3A/3B.

Holdout discipline: inputs come from phase3a_harness._load_dev_inputs, which drops every schedule row and
play from HOLDOUT_START (2023) on before any feature is computed; this module asserts it again.
"""
from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from src.nfl.backtest.phase3a_harness import _load_dev_inputs
from src.nfl.data.ingest_context import load_context
from src.nfl.data.team_aliases import normalize_team_code
from src.nfl.elo.engine import EloConfig, run_elo_walk_forward
from src.nfl.features import phase3a_config as C3
from src.nfl.features.epa_features import add_trailing_epa_features
from src.nfl.features.phase3a_features import add_phase3a_features, aggregate_style_to_game_team, normalize_schedule_teams
from src.nfl.features.phase3b_context import _pos_group, kickoff_utc
from src.nfl.features.qb_features import add_trailing_qb_features
from src.nfl.models.residual import phase3b_config as C
from src.nfl.models.total.total_model import build_total_features

HOLDOUT_START = C.HOLDOUT_START
RIDGE_ALPHA = 10.0
MIN_TRAIN_GAMES = 500
INJ_FIRST = C.INJURY_FIRST_SEASON

# ----------------------------------------------------------------------------- feature groups (section 5)
F_LINE = ["spread_line"]
F_ELO = ["elo_diff", "rest_diff"]
F_MARGIN4 = F_ELO + ["epa_net_diff", "qb_epa_diff"]
F_SCORING = ["sc_off_diff", "sc_def_diff", "epa_matchup_diff"]
F_FORM_XPL = ["form_pts_diff", "form_epa_diff", "form_sr_diff", "xpl_pass_diff", "xpl_rush_diff"]
F_QBX = ["qbx_change_diff", "qbx_backup_diff", "qbx_first_diff", "qbx_delta_diff"]
F_INJ = [f"injd_{g}" for g in C.POS_GROUPS]

# id -> (features, first_train_season)
CANDIDATES: dict[str, tuple[list[str], int]] = {
    "s1_line": (F_LINE, C.FIRST_SEASON),
    "s2_elo": (F_ELO, C.FIRST_SEASON),
    "s3_margin4": (F_MARGIN4, C.FIRST_SEASON),
    "s4_scoring": (F_MARGIN4 + F_SCORING, C.FIRST_SEASON),
    "s5_form_xpl": (F_MARGIN4 + F_FORM_XPL, C.FIRST_SEASON),
    "s6_qb": (F_MARGIN4 + F_QBX, C.FIRST_SEASON),
    "s3_2013": (F_MARGIN4, INJ_FIRST),            # identical-window control for s7_inj
    "s7_inj": (F_MARGIN4 + F_INJ, INJ_FIRST),
}


# ----------------------------------------------------------------------------- signed context builders

def build_qb_signed(games: pd.DataFrame, pbp: pd.DataFrame) -> pd.DataFrame:
    """Phase 3B's QB context (phase3b_context.build_qb) re-expressed as home minus away. Same state logic,
    same windows; the only change is that each team's flags are kept on its own side before differencing.
    State is updated with a game only after that game's features are recorded (no same-game leakage)."""
    drop = pbp[(pbp.qb_dropback == 1) & pbp.qb_epa.notna() & pbp.passer_id.notna()]
    by_game = {gid: grp.groupby("passer_id").qb_epa.mean() for gid, grp in drop.groupby("game_id")}
    g = games.copy(); g["gameday"] = pd.to_datetime(g.gameday)
    g = g.sort_values(["season", "week", "gameday", "game_id"]).reset_index(drop=True)
    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=C.QB_EPA_WINDOW))
    starts: dict[str, int] = defaultdict(int)
    team_starters: dict[str, deque] = defaultdict(lambda: deque(maxlen=C.QB_BACKUP_LOOKBACK))
    rows = []
    for r in g.itertuples(index=False):
        feats = {"game_id": r.game_id, "qbx_change_diff": 0.0, "qbx_backup_diff": 0.0,
                 "qbx_first_diff": 0.0, "qbx_delta_diff": 0.0}
        for team, qb, sign in ((r.home_team, r.home_qb_id, 1.0), (r.away_team, r.away_qb_id, -1.0)):
            fr = normalize_team_code(team); prev_list = team_starters[fr]
            if pd.isna(qb):
                continue
            prev = prev_list[-1] if prev_list else None
            if prev is not None and prev != qb:
                feats["qbx_change_diff"] += sign
                rq = float(np.mean(hist[qb])) if hist[qb] else 0.0
                rp = float(np.mean(hist[prev])) if hist[prev] else 0.0
                feats["qbx_delta_diff"] += sign * (rq - rp)
            if len(prev_list) >= C.QB_BACKUP_MIN_STARTS and sum(1 for x in prev_list if x == qb) < C.QB_BACKUP_MIN_STARTS:
                feats["qbx_backup_diff"] += sign
            if starts[qb] == 0:
                feats["qbx_first_diff"] += sign
        rows.append(feats)
        gr = by_game.get(r.game_id)
        for team, qb in ((r.home_team, r.home_qb_id), (r.away_team, r.away_qb_id)):
            if pd.isna(qb):
                continue
            team_starters[normalize_team_code(team)].append(qb); starts[qb] += 1
            if gr is not None and qb in gr.index and pd.notna(gr[qb]):
                hist[qb].append(float(gr[qb]))
    return pd.DataFrame(rows)


def build_injuries_signed(games: pd.DataFrame, injuries: pd.DataFrame, snaps: pd.DataFrame,
                          players: pd.DataFrame) -> pd.DataFrame:
    """Phase 3B's injury groups (phase3b_context.build_injuries) kept per team and differenced home minus
    away. Same status weights, same pre-kickoff filter, same 4-game snap-share importance."""
    g = games[games.season >= INJ_FIRST].copy()
    g["kick"] = kickoff_utc(g)
    long = pd.concat([g.assign(team=g.home_team, side=1.0)[["game_id", "season", "week", "team", "side", "kick", "gameday"]],
                      g.assign(team=g.away_team, side=-1.0)[["game_id", "season", "week", "team", "side", "kick", "gameday"]]])
    long["fr"] = long.team.map(normalize_team_code)
    inj = injuries[injuries.season >= INJ_FIRST].copy()
    inj = inj.drop_duplicates(["season", "week", "game_type", "team", "gsis_id"], keep="last")
    inj = inj.merge(long[["game_id", "season", "week", "team", "side", "kick"]], on=["season", "week", "team"], how="left")
    inj = inj[inj.game_id.notna()]
    inj = inj[~(inj.date_modified >= inj.kick)]
    inj["w"] = pd.to_numeric(inj.report_status.map(C.STATUS_WEIGHT), errors="coerce").astype("float64").fillna(0.0)
    inj["grp"] = inj.position.map(_pos_group)
    inj = inj[(inj.w > 0) & inj.grp.notna()]
    ids = players.dropna(subset=["pfr_id"]).drop_duplicates("gsis_id").set_index("gsis_id").pfr_id
    inj["pfr_id"] = inj.gsis_id.map(ids)
    sn = snaps.copy(); sn["pct"] = sn[["offense_pct", "defense_pct"]].max(axis=1)
    pct = sn.set_index(["game_id", "pfr_player_id"]).pct
    pct = pct[~pct.index.duplicated()].to_dict()
    order = long.sort_values(["gameday", "game_id"])
    team_games = {fr: grp.game_id.tolist() for fr, grp in order.groupby("fr")}
    pos_in = {fr: {gid: i for i, gid in enumerate(lst)} for fr, lst in team_games.items()}
    inj["fr"] = inj.team.map(normalize_team_code)

    def importance(row):
        if pd.isna(row.pfr_id):
            return 0.0
        lst = team_games[row.fr]; i = pos_in[row.fr][row.game_id]
        prev = lst[max(0, i - C.SNAP_WINDOW_GAMES):i]
        return float(np.mean([pct.get((pg, row.pfr_id), 0.0) for pg in prev])) if prev else 0.0
    inj["imp"] = [importance(r) for r in inj.itertuples(index=False)]
    inj["score"] = inj.w * inj.imp * inj.side          # +home, -away
    agg = inj.groupby(["game_id", "grp"]).score.sum().unstack(fill_value=0.0)
    out = pd.DataFrame({"game_id": g.game_id.values})
    for grp in C.POS_GROUPS:
        out[f"injd_{grp}"] = out.game_id.map(agg[grp]).fillna(0.0) if grp in agg else 0.0
    return out


# ----------------------------------------------------------------------------- frame

def build_spread_frame(raw_dir: Path | None = None, holdout_start: int = HOLDOUT_START) -> pd.DataFrame:
    games, pbp, style = _load_dev_inputs(raw_dir, holdout_start)
    gn = normalize_schedule_teams(games)                      # franchise-continuous codes (STL/SD/OAK fix)
    g1 = add_trailing_qb_features(add_trailing_epa_features(gn, pbp, window=10), pbp, window=10)
    df = build_total_features(g1, window=16)
    elo, _ = run_elo_walk_forward(gn, EloConfig())
    df = df.merge(elo[["game_id", "pre_home_elo", "pre_away_elo"]], on="game_id", how="left", validate="one_to_one")
    game_team = aggregate_style_to_game_team(style, games)
    p3 = add_phase3a_features(gn, game_team, half_life=C3.HALF_LIFE_PRIMARY)
    df = df.merge(p3[["game_id"] + C3.GROUP_A + C3.GROUP_C], on="game_id", how="left", validate="one_to_one")
    df = df.merge(build_qb_signed(gn, pbp), on="game_id", how="left", validate="one_to_one")
    ctx = load_context(raw_dir) if raw_dir else load_context()
    df = df.merge(build_injuries_signed(gn, ctx["injuries"], ctx["snap_counts"], ctx["players"]),
                  on="game_id", how="left", validate="one_to_one")

    df["elo_diff"] = df["pre_home_elo"] - df["pre_away_elo"]
    df["rest_diff"] = (pd.to_numeric(df["home_rest"], errors="coerce").fillna(7)
                       - pd.to_numeric(df["away_rest"], errors="coerce").fillna(7)).clip(-10, 10)
    df["epa_net_diff"] = ((df["home_off_epa_trailing"] - df["home_def_epa_allowed_trailing"])
                          - (df["away_off_epa_trailing"] - df["away_def_epa_allowed_trailing"]))
    df["qb_epa_diff"] = df["home_qb_epa_trailing"] - df["away_qb_epa_trailing"]
    df["sc_off_diff"] = df["home_off_trailing"] - df["away_off_trailing"]
    df["sc_def_diff"] = df["home_def_trailing"] - df["away_def_trailing"]
    df["epa_matchup_diff"] = df["home_epa_matchup"] - df["away_epa_matchup"]
    df["form_pts_diff"] = df["form_pts_home"] - df["form_pts_away"]
    df["form_epa_diff"] = df["form_epa_home"] - df["form_epa_away"]
    df["form_sr_diff"] = df["form_sr_home"] - df["form_sr_away"]
    df["xpl_pass_diff"] = df["xpl_pass_home"] - df["xpl_pass_away"]
    df["xpl_rush_diff"] = df["xpl_rush_home"] - df["xpl_rush_away"]
    df["margin"] = df["home_score"] - df["away_score"]
    df["spread_residual"] = df["margin"] - df["spread_line"]
    # restore schedule-of-the-day team codes for output
    df = df.drop(columns=["home_team", "away_team"]).merge(games[["game_id", "home_team", "away_team"]], on="game_id", how="left")
    assert df["season"].max() < holdout_start, "holdout season present"
    return df


# ----------------------------------------------------------------------------- walk-forward

def walk_forward_spread_residual(df: pd.DataFrame, features: list[str], first_train_season: int = C.FIRST_SEASON,
                                 alpha: float = RIDGE_ALPHA, min_train_games: int = MIN_TRAIN_GAMES) -> pd.DataFrame:
    """Adds r_hat and r_sigma. For season S: fit on seasons in [first_train_season, S) with a known margin and
    closing spread, standardize on that fold, Ridge(alpha). Test rows need a closing spread (the target is
    only defined against it) and every feature present."""
    df = df.sort_values(["season", "week", "gameday", "game_id"]).reset_index(drop=True)
    assert df["season"].max() < HOLDOUT_START
    r_hat = pd.Series(np.nan, index=df.index); r_sig = pd.Series(np.nan, index=df.index)
    log = {}
    for season in sorted(df["season"].unique()):
        train = df[(df["season"] < season) & (df["season"] >= first_train_season)
                   & df["spread_residual"].notna()].dropna(subset=features)
        if len(train) < min_train_games:
            continue
        test = df[(df["season"] == season) & df["spread_line"].notna()].dropna(subset=features)
        if len(test) == 0:
            continue
        X = train[features].to_numpy(float); y = train["spread_residual"].to_numpy(float)
        sc = StandardScaler().fit(X)
        m = Ridge(alpha=alpha).fit(sc.transform(X), y)
        resid = y - m.predict(sc.transform(X))
        r_hat.loc[test.index] = m.predict(sc.transform(test[features].to_numpy(float)))
        r_sig.loc[test.index] = float(np.std(resid, ddof=1))
        log[int(season)] = {"n_train": int(len(train)), "intercept": float(m.intercept_),
                            "coef_std": dict(zip(features, map(float, m.coef_)))}
    out = df.copy(); out["r_hat"] = r_hat; out["r_sigma"] = r_sig
    out.attrs["fit_log"] = log
    return out
