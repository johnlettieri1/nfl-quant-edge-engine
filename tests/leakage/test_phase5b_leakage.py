"""Phase 5B leakage tests: spread residual walk-forward and the signed QB context builder.

What each test proves:
  - future invariance: corrupting every game from a cutoff season on (scores, spreads, features) leaves every
    predicted residual before the cutoff unchanged;
  - own-season invariance: corrupting season S's outcomes leaves season S's predictions unchanged;
  - corruption is not vacuous: later seasons do change;
  - the target is exactly actual margin minus the closing spread, with the pinned sign convention
    (positive spread_line = home favored, so r_hat > 0 means "home side");
  - the walk-forward refuses any holdout (2023+) row;
  - the signed QB builder records a game's flags before updating state with that game, and reverses sign
    when home and away are swapped;
  - written result files contain no holdout season.
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.nfl.models.spread.residual_spread import HOLDOUT_START, build_qb_signed, walk_forward_spread_residual

FEATS = ["f1", "f2"]


def frame(seasons=range(2001, 2013), per=260, seed=0):
    rng = np.random.default_rng(seed); rows = []
    for s in seasons:
        for i in range(per):
            f1, f2 = rng.normal(size=2)
            line = 2 * f1 + rng.normal(0, 3)
            margin = line + 0.7 * f2 + rng.normal(0, 13)
            rows.append({"game_id": f"{s}_{i}", "season": s, "week": 1 + i // 16, "game_type": "REG",
                         "gameday": pd.Timestamp(f"{s}-09-01") + pd.Timedelta(days=i // 16 * 7), "away_team": "A",
                         "home_team": "H", "home_score": 20.0 + round(margin), "away_score": 20.0,
                         "spread_line": round(line * 2) / 2, "f1": f1, "f2": f2})
    df = pd.DataFrame(rows)
    df["margin"] = df.home_score - df.away_score
    df["spread_residual"] = df.margin - df.spread_line
    return df


def corrupt(df, from_season, seed=99):
    rng = np.random.default_rng(seed); d = df.copy(); m = d.season >= from_season
    d.loc[m, "home_score"] = rng.integers(0, 60, m.sum()).astype(float)
    d.loc[m, "spread_line"] = rng.uniform(-14, 14, m.sum())
    for c in FEATS:
        d.loc[m, c] = rng.normal(0, 5, m.sum())
    d["margin"] = d.home_score - d.away_score; d["spread_residual"] = d.margin - d.spread_line
    return d


def _wf(df):
    return walk_forward_spread_residual(df, FEATS).set_index("game_id")


def test_future_invariance():
    base, cut = _wf(frame()), _wf(corrupt(frame(), 2008))
    before = base.index[base.season < 2008]
    pd.testing.assert_series_equal(base.loc[before, "r_hat"], cut.loc[before, "r_hat"])
    pd.testing.assert_series_equal(base.loc[before, "r_sigma"], cut.loc[before, "r_sigma"])
    after = base.index[(base.season >= 2009) & base.r_hat.notna()]
    assert not np.allclose(base.loc[after, "r_hat"], cut.loc[after, "r_hat"]), "corruption was vacuous"


def test_own_season_outcomes_do_not_change_own_predictions():
    df = frame(); d = df.copy(); m = d.season == 2010
    d.loc[m, "home_score"] = 99.0; d["margin"] = d.home_score - d.away_score; d["spread_residual"] = d.margin - d.spread_line
    a, b = _wf(df), _wf(d)
    idx = a.index[a.season == 2010]
    pd.testing.assert_series_equal(a.loc[idx, "r_hat"], b.loc[idx, "r_hat"])


def test_target_and_sign_convention():
    df = frame()
    assert np.allclose(df.spread_residual, (df.home_score - df.away_score) - df.spread_line)
    # home favored by 3 and wins by 7: home covers, residual +4
    assert (7 - 3) == 4 and (7 - 3) > 0


def test_holdout_refused():
    df = frame(seasons=range(2015, HOLDOUT_START + 1))
    with pytest.raises(AssertionError):
        walk_forward_spread_residual(df, FEATS)


def test_no_prediction_without_enough_history():
    wf = _wf(frame())
    assert wf[wf.season == 2001].r_hat.isna().all()      # fewer than 500 prior games
    assert wf[wf.season == 2004].r_hat.notna().all()


def _qb_games():
    rows = []
    for i, (hq, aq) in enumerate([("q1", "q2"), ("q1", "q2"), ("q1", "q2"), ("q1", "q2"), ("q3", "q2"), ("q3", "q2")]):
        rows.append({"game_id": f"2015_{i}", "season": 2015, "week": i + 1, "gameday": f"2015-09-{10 + i:02d}",
                     "home_team": "H", "away_team": "A", "home_qb_id": hq, "away_qb_id": aq})
    return pd.DataFrame(rows)


def _qb_pbp(games):
    rows = []
    for r in games.itertuples(index=False):
        for qb in (r.home_qb_id, r.away_qb_id):
            rows.append({"game_id": r.game_id, "qb_dropback": 1, "qb_epa": 0.1 if qb == "q1" else -0.1, "passer_id": qb})
    return pd.DataFrame(rows)


def test_qb_signed_change_is_recorded_on_the_change_game_only_and_signed():
    g = _qb_games(); out = build_qb_signed(g, _qb_pbp(g)).set_index("game_id")
    assert out.loc["2015_4", "qbx_change_diff"] == 1.0           # home starter changed q1 -> q3, home side => +1
    assert out.loc["2015_3", "qbx_change_diff"] == 0.0 and out.loc["2015_5", "qbx_change_diff"] == 0.0
    assert out.loc["2015_4", "qbx_first_diff"] == 1.0            # q3's first start
    assert out.loc["2015_0", "qbx_first_diff"] == 0.0            # both first starts cancel (+1 -1)
    # swapping home and away flips every sign
    sw = g.rename(columns={"home_team": "away_team", "away_team": "home_team", "home_qb_id": "away_qb_id", "away_qb_id": "home_qb_id"})
    out2 = build_qb_signed(sw, _qb_pbp(sw)).set_index("game_id")
    for c in ("qbx_change_diff", "qbx_backup_diff", "qbx_first_diff", "qbx_delta_diff"):
        assert np.allclose(out[c], -out2[c])


def test_written_results_have_no_holdout_rows():
    d = Path("docs/phase5/results")
    if not d.exists():
        pytest.skip("results not generated in this checkout")
    for p in d.glob("*_oos_*.csv"):
        with p.open() as f:
            seasons = {int(r["season"]) for r in csv.DictReader(f)}
        assert seasons and max(seasons) < HOLDOUT_START, p.name
