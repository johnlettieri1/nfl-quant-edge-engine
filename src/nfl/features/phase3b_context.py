"""Phase 3B-3 context features (spec section 5): weather (W), injuries (I), quarterback (Q).

All three return one row per game_id. Timing labels:
  W  observed at kickoff (historical research availability only; NOT a forecast)
  I  final pregame injury report rows with date_modified < kickoff; snap shares from earlier games only
  Q  actual starter (known by the inactive list, ~90 min before kickoff); QB EPA from earlier games only
"""
from __future__ import annotations

import re
from collections import defaultdict, deque

import numpy as np
import pandas as pd

from src.nfl.data.team_aliases import normalize_team_code
from src.nfl.models.residual import phase3b_config as C

# ----------------------------------------------------------------------------- weather

_CHANCE = re.compile(r"(\d{1,3})\s*%\s*chance\s+of\s+[a-z ]+?(?=[,.;]|temp|$)", re.I)
_PRECIP = re.compile(r"\b(rain|raining|rainy|showers?|drizzle|snow|snowing|snowy|sleet|flurries|thunderstorms?|storms?)\b", re.I)
_SNOW = re.compile(r"\b(snow|snowing|snowy|flurries|sleet)\b", re.I)
_TEMP = re.compile(r"temp[^0-9-]*(-?\d{1,3})", re.I)
_WIND = re.compile(r"wind[^0-9]*?(\d{1,2})\s*(?:mph|[nsew])", re.I)


def parse_weather_text(text) -> dict:
    if not isinstance(text, str):
        return {"precip": np.nan, "snow": np.nan, "temp_txt": np.nan, "wind_txt": np.nan}
    t = _CHANCE.sub(lambda m: "" if int(m.group(1)) < C.PRECIP_CHANCE_IGNORE_BELOW else m.group(0), text)
    tm = _TEMP.search(text); wm = _WIND.search(text)
    return {"precip": float(bool(_PRECIP.search(t))), "snow": float(bool(_SNOW.search(t))),
            "temp_txt": float(tm.group(1)) if tm else np.nan, "wind_txt": float(wm.group(1)) if wm else np.nan}


def build_weather(games: pd.DataFrame, weather_text: pd.DataFrame, train_median_wind: float = 8.0,
                  train_median_temp: float = 59.0) -> pd.DataFrame:
    g = games[["game_id", "roof", "temp", "wind"]].merge(weather_text, on="game_id", how="left")
    parsed = pd.DataFrame([parse_weather_text(x) for x in g.weather], index=g.index)
    g = pd.concat([g, parsed], axis=1)
    outdoor = g.roof.isin(C.OUTDOOR_ROOFS)
    temp = pd.to_numeric(g.temp, errors="coerce").fillna(g.temp_txt)
    wind = pd.to_numeric(g.wind, errors="coerce").fillna(g.wind_txt)
    missing = outdoor & (temp.isna() | wind.isna())
    temp = temp.fillna(train_median_temp); wind = wind.fillna(train_median_wind).clip(upper=C.WIND_CAP)
    precip = g.precip.fillna(0.0); snow = g.snow.fillna(0.0)
    o = outdoor.astype(float)
    out = pd.DataFrame({"game_id": g.game_id,
                        "wx_wind": o * wind,
                        "wx_wind15": o * (wind >= C.WIND_STRONG),
                        "wx_extreme": o * ((wind >= C.WIND_EXTREME) | (temp <= C.TEMP_EXTREME) | (snow > 0)),
                        "wx_cold": o * np.maximum(0.0, C.TEMP_COLD_REF - temp) / 10.0,
                        "wx_precip": o * precip, "wx_snow": o * snow, "wx_missing": missing.astype(float)})
    return out


# ----------------------------------------------------------------------------- kickoff time

def kickoff_utc(games: pd.DataFrame) -> pd.Series:
    t = games.gametime.fillna("13:00")
    local = pd.to_datetime(pd.to_datetime(games.gameday).dt.strftime("%Y-%m-%d") + " " + t)
    return local.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")


# ----------------------------------------------------------------------------- injuries

def _pos_group(pos) -> str | None:
    for g, ps in C.POS_GROUPS.items():
        if pos in ps:
            return g
    return None


def build_injuries(games: pd.DataFrame, injuries: pd.DataFrame, snaps: pd.DataFrame, players: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Returns (features per game, audit dict). Games before INJURY_FIRST_SEASON get NaN (not used)."""
    g = games[games.season >= C.INJURY_FIRST_SEASON].copy()
    g["kick"] = kickoff_utc(g)
    long = pd.concat([g.assign(team=g.home_team)[["game_id", "season", "week", "team", "kick", "gameday"]],
                      g.assign(team=g.away_team)[["game_id", "season", "week", "team", "kick", "gameday"]]])
    long["fr"] = long.team.map(normalize_team_code)
    inj = injuries[injuries.season >= C.INJURY_FIRST_SEASON].copy()
    inj = inj.drop_duplicates(["season", "week", "game_type", "team", "gsis_id"], keep="last")
    inj = inj.merge(long[["game_id", "season", "week", "team", "kick"]], on=["season", "week", "team"], how="left")
    audit = {"report_rows": int(len(inj)), "rows_unmatched_to_game": int(inj.game_id.isna().sum())}
    inj = inj[inj.game_id.notna()]
    late = inj.date_modified >= inj.kick
    audit["rows_modified_at_or_after_kickoff_dropped"] = int(late.sum())
    hrs = (inj.kick - inj.date_modified).dt.total_seconds() / 3600
    audit["hours_before_kickoff_median"] = float(hrs[~late].median())
    audit["share_modified_within_24h_of_kickoff"] = float(((hrs >= 0) & (hrs < 24)).mean())
    inj = inj[~late]
    inj["w"] = pd.to_numeric(inj.report_status.map(C.STATUS_WEIGHT), errors="coerce").astype("float64").fillna(0.0)  # pandas 3.1-safe
    inj["grp"] = inj.position.map(_pos_group)
    inj = inj[(inj.w > 0) & inj.grp.notna()]
    ids = players.dropna(subset=["pfr_id"]).drop_duplicates("gsis_id").set_index("gsis_id").pfr_id
    inj["pfr_id"] = inj.gsis_id.map(ids)
    audit["weighted_rows_without_pfr_id"] = int(inj.pfr_id.isna().sum())
    # snap share lookup and each franchise's chronological game list
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
        if not prev:
            return 0.0
        return float(np.mean([pct.get((pg, row.pfr_id), 0.0) for pg in prev]))
    inj["imp"] = [importance(r) for r in inj.itertuples(index=False)]
    inj["score"] = inj.w * inj.imp
    agg = inj.groupby(["game_id", "grp"]).score.sum().unstack(fill_value=0.0)
    out = pd.DataFrame({"game_id": g.game_id.values})
    for grp in C.POS_GROUPS:
        out[f"inj_{grp}"] = out.game_id.map(agg[grp]).fillna(0.0) if grp in agg else 0.0
    audit["games"] = int(len(out)); audit["mean_by_group"] = {c: float(out[c].mean()) for c in C.INJURY_FEATURES}
    return out, audit


# ----------------------------------------------------------------------------- quarterback

def build_qb(games: pd.DataFrame, pbp: pd.DataFrame) -> pd.DataFrame:
    drop = pbp[(pbp.qb_dropback == 1) & pbp.qb_epa.notna() & pbp.passer_id.notna()]
    by_game = {gid: grp.groupby("passer_id").qb_epa.mean() for gid, grp in drop.groupby("game_id")}
    g = games.copy(); g["gameday"] = pd.to_datetime(g.gameday)
    g = g.sort_values(["season", "week", "gameday", "game_id"]).reset_index(drop=True)
    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=C.QB_EPA_WINDOW))
    starts: dict[str, int] = defaultdict(int)
    team_starters: dict[str, deque] = defaultdict(lambda: deque(maxlen=C.QB_BACKUP_LOOKBACK))
    rows = []
    for r in g.itertuples(index=False):
        feats = {"game_id": r.game_id, "qb_change": 0.0, "qb_backup": 0.0, "qb_first_start": 0.0, "qb_delta": 0.0}
        for team, qb in ((r.home_team, r.home_qb_id), (r.away_team, r.away_qb_id)):
            fr = normalize_team_code(team); prev_list = team_starters[fr]
            if pd.isna(qb):
                continue
            prev = prev_list[-1] if prev_list else None
            if prev is not None and prev != qb:
                feats["qb_change"] += 1
                rq = float(np.mean(hist[qb])) if hist[qb] else 0.0
                rp = float(np.mean(hist[prev])) if hist[prev] else 0.0
                feats["qb_delta"] += rq - rp
            if len(prev_list) >= C.QB_BACKUP_MIN_STARTS and sum(1 for x in prev_list if x == qb) < C.QB_BACKUP_MIN_STARTS:
                feats["qb_backup"] += 1
            if starts[qb] == 0:
                feats["qb_first_start"] += 1
        rows.append(feats)
        # update state with this game only after its features are recorded
        gr = by_game.get(r.game_id)
        for team, qb in ((r.home_team, r.home_qb_id), (r.away_team, r.away_qb_id)):
            if pd.isna(qb):
                continue
            team_starters[normalize_team_code(team)].append(qb); starts[qb] += 1
            if gr is not None and qb in gr.index and pd.notna(gr[qb]):
                hist[qb].append(float(gr[qb]))
    return pd.DataFrame(rows)
