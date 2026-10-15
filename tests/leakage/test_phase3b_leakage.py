"""Phase 3B leakage tests: residual target, nested walk-forward, calibration layer.

What each test proves:
  - future invariance: corrupting every game after a cutoff season (scores, lines, features)
    leaves every predicted residual and every calibrated probability up to the cutoff unchanged,
    including the nested alpha choice and nested group selection;
  - own-season invariance: corrupting the outcomes of season S leaves season S's predicted
    residuals AND its calibrated probabilities unchanged (no observation calibrates itself);
  - corruption is not vacuous: later seasons do change;
  - the target is exactly actual total minus closing total;
  - the holdout guard refuses any 2023+ row.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.nfl.backtest.phase3b_harness import finalize, write
from src.nfl.models.residual.calibration import no_vig_over, walk_forward_calibrate
from src.nfl.models.residual.residual_model import add_residual_target, walk_forward_residual

FEATS = ["total_line", "f1", "f2"]
GROUPS = {"g1": ["f3"], "g2": ["f4"]}


def frame(seasons=range(2001, 2013), per=260, seed=0):
    rng = np.random.default_rng(seed); rows = []; gid = 0
    for s in seasons:
        for i in range(per):
            gid += 1
            f1, f2, f3, f4 = rng.normal(size=4)
            line = 44 + 3 * f1 + rng.normal(0, 2)
            pts = line + 0.8 * f3 + rng.normal(0, 13)
            rows.append({"game_id": f"{s}_{i}", "season": s, "week": 1 + i // 16, "game_type": "REG",
                         "gameday": pd.Timestamp(f"{s}-09-01") + pd.Timedelta(days=i // 16 * 7), "away_team": "A",
                         "home_team": "H", "total_points": float(round(pts)), "total_line": round(line * 2) / 2,
                         "over_odds": -110, "under_odds": -110, "f1": f1, "f2": f2, "f3": f3, "f4": f4})
    return pd.DataFrame(rows)


def corrupt(df, from_season, seed=99):
    rng = np.random.default_rng(seed); d = df.copy(); m = d.season >= from_season
    d.loc[m, "total_points"] = rng.integers(0, 100, m.sum()).astype(float)
    d.loc[m, "total_line"] = rng.uniform(30, 60, m.sum())
    for c in ("f1", "f2", "f3", "f4"):
        d.loc[m, c] = rng.normal(0, 5, m.sum())
    return d


def corrupt_outcomes_only(df, season, seed=7):
    rng = np.random.default_rng(seed); d = df.copy(); m = d.season == season
    d.loc[m, "total_points"] = rng.integers(0, 100, m.sum()).astype(float)
    return d


def _wf(df, groups=None):
    return walk_forward_residual(df, FEATS, select_groups=groups).set_index("game_id")


@pytest.mark.parametrize("groups", [None, GROUPS])
def test_residual_future_invariance(groups):
    base = frame(); cut = 2009
    a = _wf(base, groups); b = _wf(corrupt(base, cut + 1), groups)
    keep = a.index[a.season <= cut]
    assert a.loc[keep, "r_hat"].notna().sum() > 1000
    pd.testing.assert_series_equal(a.loc[keep, "r_hat"], b.loc[keep, "r_hat"])
    later = a.index[a.season > cut]
    assert (a.loc[later, "r_hat"] - b.loc[later, "r_hat"]).abs().max() > 1e-6  # not vacuous
    if groups:
        la, lb = a.attrs["fit_log"], b.attrs["fit_log"]
        assert all(la[s] == lb[s] for s in la if s <= cut)


@pytest.mark.parametrize("groups", [None, GROUPS])
def test_residual_own_season_invariance(groups):
    base = frame(); s = 2010
    a = _wf(base, groups); b = _wf(corrupt_outcomes_only(base, s), groups)
    idx = a.index[a.season == s]
    pd.testing.assert_series_equal(a.loc[idx, "r_hat"], b.loc[idx, "r_hat"])
    assert (a.loc[a.season > s, "r_hat"] - b.loc[b.season > s, "r_hat"]).abs().max() > 1e-6


def test_residual_target_definition():
    d = add_residual_target(frame(seasons=[2005], per=20))
    assert np.allclose(d.residual, d.total_points - d.total_line)


def _cal(df):
    wf = walk_forward_residual(df, FEATS)
    return finalize(wf, "r_hat", "r_sigma", "t").set_index("game_id")


def test_calibration_future_and_own_season_invariance():
    base = frame(); cut = 2009
    a = _cal(base); b = _cal(corrupt(base, cut + 1))
    keep = a.index[a.season <= cut]
    for c in ("p_raw", "p_logistic", "p_shrink", "p_isotonic"):
        pd.testing.assert_series_equal(a.loc[keep, c], b.loc[keep, c])
    # outcomes of season s corrupted -> season s probabilities unchanged (the calibrator never saw them)
    s = 2010; c2 = _cal(corrupt_outcomes_only(base, s)); idx = a.index[a.season == s]
    for c in ("p_logistic", "p_shrink", "p_isotonic"):
        pd.testing.assert_series_equal(a.loc[idx, c], c2.loc[idx, c])
        assert (a.loc[a.season > s, c] - c2.loc[c2.season > s, c]).abs().max() > 1e-9


def test_calibrator_trains_only_on_prior_seasons():
    f = pd.DataFrame({"season": np.repeat(np.arange(2001, 2006), 600), "r": np.tile(np.linspace(-3, 3, 600), 5),
                      "r_sigma": 13.0})
    f["over"] = (f.r > 0).astype(float)
    out = walk_forward_calibrate(f)
    log = out.attrs["cal_log"]
    assert log[2001]["fallback"] is True and (out[out.season == 2001].p_logistic == 0.5).all()
    for s, v in log.items():
        if not v.get("fallback"):
            assert v["cal_seasons"][1] == s - 1


def test_no_vig():
    p = no_vig_over(pd.Series([-110, -120, None, 100]), pd.Series([-110, 100, -110, -100]))
    assert np.isclose(p[0], 0.5) and p[1] > 0.5 and p[2] == 0.5 and np.isclose(p[3], 0.5)


def test_holdout_guard(tmp_path):
    f = frame(seasons=[2022, 2023], per=10)
    f["r"] = 0.0; f["residual"] = 0.0
    with pytest.raises(AssertionError):
        write({"x": f}, {}, tmp_path, "t")
