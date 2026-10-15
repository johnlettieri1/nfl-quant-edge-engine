"""Phase 3B-3 leakage and timing tests: weather, injuries, quarterback context.

  - injuries: a report row modified at or after kickoff has no effect; a game's own snap counts
    never feed its own injury importance; corrupting later games leaves earlier features unchanged;
  - QB: a game's own QB EPA and later starters never change its features;
  - weather: indoor games carry no weather signal; parsing ignores low "% chance" phrases;
  - research context pulls refuse holdout seasons.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.nfl.data.ingest_context import _guard
from src.nfl.features.phase3b_context import build_injuries, build_qb, build_weather, parse_weather_text


def sched(n=8):
    rows = []
    for i in range(n):
        home, away = ("AAA", "BBB") if i % 2 == 0 else ("BBB", "AAA")
        rows.append({"game_id": f"2015_{i+1:02d}_{away}_{home}", "season": 2015, "week": i + 1, "game_type": "REG",
                     "gameday": pd.Timestamp("2015-09-13") + pd.Timedelta(days=7 * i), "gametime": "13:00",
                     "home_team": home, "away_team": away, "roof": "outdoors", "temp": 50.0, "wind": 10.0,
                     "home_qb_id": "Q_" + home, "away_qb_id": "Q_" + away})
    return pd.DataFrame(rows)


def snaps(g, seed=0):
    rng = np.random.default_rng(seed); rows = []
    for r in g.itertuples(index=False):
        for team in (r.home_team, r.away_team):
            for p in range(3):
                rows.append({"game_id": r.game_id, "pfr_player_id": f"{team}_p{p}", "team": team,
                             "offense_pct": float(rng.uniform(0.5, 1.0)), "defense_pct": 0.0})
    return pd.DataFrame(rows)


def injuries(g):
    rows = []
    for r in g.itertuples(index=False):
        kick = pd.Timestamp(r.gameday).tz_localize("America/New_York") + pd.Timedelta(hours=13)
        rows.append({"season": 2015, "week": r.week, "game_type": "REG", "team": r.home_team, "gsis_id": f"G_{r.home_team}_p0",
                     "position": "WR", "report_status": "Out", "date_modified": (kick - pd.Timedelta(hours=50)).tz_convert("UTC")})
    return pd.DataFrame(rows)


def players():
    return pd.DataFrame({"gsis_id": [f"G_{t}_p{p}" for t in ("AAA", "BBB") for p in range(3)],
                         "pfr_id": [f"{t}_p{p}" for t in ("AAA", "BBB") for p in range(3)]})


def test_injury_own_game_and_future_snaps_unused():
    g = sched(); s = snaps(g); a, _ = build_injuries(g, injuries(g), s, players())
    gid = g.game_id[4]
    s2 = s.copy(); s2.loc[s2.game_id.isin(g.game_id[4:]), "offense_pct"] = 0.0
    b, _ = build_injuries(g, injuries(g), s2, players())
    a, b = a.set_index("game_id"), b.set_index("game_id")
    assert a.loc[gid, "inj_wrte"] == b.loc[gid, "inj_wrte"] > 0           # own-game and later snaps unused
    assert (a.loc[g.game_id[:5]] == b.loc[g.game_id[:5]]).all().all()
    assert (a.loc[g.game_id[6:], "inj_wrte"] != b.loc[g.game_id[6:], "inj_wrte"]).any()  # not vacuous


def test_injury_rows_after_kickoff_ignored():
    g = sched(); s = snaps(g); inj = injuries(g)
    late = inj.copy(); late["date_modified"] = late.date_modified + pd.Timedelta(hours=60)
    f, audit = build_injuries(g, late, s, players())
    assert audit["rows_modified_at_or_after_kickoff_dropped"] == len(inj)
    assert (f.inj_wrte == 0).all()


def test_qb_features_ignore_own_and_future_games():
    g = sched()
    g.loc[5, "home_qb_id"] = "BACKUP"
    pbp = pd.DataFrame([{"game_id": r.game_id, "passer_id": q, "qb_dropback": 1, "qb_epa": 0.1}
                        for r in g.itertuples() for q in (r.home_qb_id, r.away_qb_id)])
    a = build_qb(g, pbp).set_index("game_id")
    pbp2 = pbp.copy(); pbp2.loc[pbp2.game_id.isin(g.game_id[5:]), "qb_epa"] = 9.0
    g2 = g.copy(); g2.loc[6:, "home_qb_id"] = "SOMEONE_ELSE"
    b = build_qb(g2, pbp2).set_index("game_id")
    pd.testing.assert_frame_equal(a.loc[g.game_id[:6]], b.loc[g.game_id[:6]])
    assert a.loc[g.game_id[5], "qb_change"] == 1 and a.loc[g.game_id[5], "qb_first_start"] == 1


def test_weather_indoor_zero_and_parse():
    g = sched(2); g.loc[0, "roof"] = "dome"; g.loc[:, "wind"] = 25.0
    w = build_weather(g, pd.DataFrame({"game_id": g.game_id, "weather": ["Snow", "Snow"]})).set_index("game_id")
    assert (w.loc[g.game_id[0]] == 0).all()
    assert w.loc[g.game_id[1], "wx_extreme"] == 1 and w.loc[g.game_id[1], "wx_snow"] == 1
    assert parse_weather_text("0% Chance of Rain Temp: 77")["precip"] == 0
    assert parse_weather_text("Rain, Temp: 40")["precip"] == 1


def test_context_pull_refuses_holdout():
    with pytest.raises(ValueError):
        _guard(2023, allow_live=False)
    _guard(2022, allow_live=False)
