"""Calibrated probability display (ridge8_v1_shrink_cal_v1): proves the change is reporting only.

  - projected_total, sigma, edge, lean and model_over_prob are exactly what the pre-change code computes
  - raw_model_probability == model_over_prob; calibrated_probability = frozen shrink of it
  - the lean can never flip; recommended_units stays 0
  - calibration refuses any training season after 2022 (fit, load and data build)
  - existing snapshot files are never overwritten; new files land only under data/predictions
  - the weekly workflow's records guard still admits only data/odds and data/predictions
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, strategies as st
from scipy.stats import norm

from src.nfl.features.epa_features import add_trailing_epa_features
from src.nfl.features.qb_features import add_trailing_qb_features
from src.nfl.models.total import calibration as cal
from src.nfl.models.total.production import fit_and_predict_total
from src.nfl.reporting import predict_totals as pt
from tests.leakage._helpers import fake_elo_games, fake_pbp_for_games

ROOT = Path(__file__).resolve().parents[2]
RAW_COLS = ["game_id", "season", "week", "gameday", "game_type", "home_team", "away_team", "home_score", "away_score",
            "roof", "total_line", "home_qb_id", "away_qb_id"]


@pytest.fixture()
def fixture(monkeypatch):
    g = fake_elo_games(n_seasons=4, games_per_season=110, seed=11)
    pbp = fake_pbp_for_games(g, seed=11)
    games = g[RAW_COLS].copy()
    rng = np.random.default_rng(5)
    games["total_line"] = np.round(rng.normal(44, 4, len(games)) * 2) / 2
    games["over_odds"], games["under_odds"], games["gametime"] = -110, -110, "13:00"
    last = games.index[-12:]
    games.loc[last, ["home_score", "away_score"]] = np.nan
    games.loc[last, "week"] = 500
    monkeypatch.setattr(pt, "load_cached_schedules", lambda: games.copy())
    monkeypatch.setattr(pt, "load_cached_pbp_for_epa", lambda: pbp.copy())
    monkeypatch.setattr(pt, "load_quotes", lambda: pd.DataFrame())  # Phase 4: paired quotes replace game_summary
    return games, pbp


def _reference(games, pbp):
    """The pre-change computation, written out independently."""
    g = games.copy(); g["gameday"] = pd.to_datetime(g.gameday)
    df = fit_and_predict_total(add_trailing_qb_features(add_trailing_epa_features(g, pbp, window=10), pbp, window=10), window=16)
    out = {}
    for r in df[df.home_score.isna()].itertuples():
        proj, sigma = round(float(r.total_mean_pred), 2), round(float(r.total_sigma_pred), 2)
        edge = round(proj - float(r.total_line), 2)
        out[r.game_id] = {"projected_total": proj, "sigma": sigma, "edge": edge,
                          "lean": "OVER" if edge > 0 else "UNDER" if edge < 0 else "PASS",
                          "model_over_prob": round(float(1 - norm.cdf((float(r.total_line) - proj) / sigma)), 4)}
    return out


def _snapshot(tmp_path):
    out = pt.generate(pred_dir=tmp_path / "data" / "predictions")
    return out, json.loads(out.read_text())


def test_projection_edge_lean_and_raw_probability_unchanged(fixture, tmp_path):
    ref = _reference(*fixture)
    _, snap = _snapshot(tmp_path)
    assert len(snap["games"]) == 12 and {g["lean"] for g in snap["games"]} == {"OVER", "UNDER"}
    for g in snap["games"]:
        r = ref[g["game_id"]]
        for k in ("projected_total", "sigma", "edge", "lean", "model_over_prob"):
            assert g[k] == r[k], k
        assert g["raw_model_probability"] == g["model_over_prob"]
        f = snap["probability_calibration"]["shrink_factor"]
        assert g["calibrated_probability"] == round(0.5 + f * (g["model_over_prob"] - 0.5), 4)


def test_units_zero_lean_never_flips_and_metadata(fixture, tmp_path):
    _, snap = _snapshot(tmp_path)
    meta = snap["probability_calibration"]
    assert meta["method"] == "shrink_toward_0.5" and meta["calibration_version"] == "ridge8_v1_shrink_cal_v1"
    assert meta["training_seasons"][1] <= 2022 and 0 < meta["shrink_factor"] < 1
    assert snap["model_version"] == "ridge8_v1" and snap["display_probability_field"] == "calibrated_probability"
    for g in snap["games"]:
        assert g["recommended_units"] == 0.0
        side_raw = np.sign(g["raw_model_probability"] - 0.5); side_cal = np.sign(g["calibrated_probability"] - 0.5)
        assert side_raw == side_cal
        assert abs(g["calibrated_probability"] - 0.5) <= abs(g["raw_model_probability"] - 0.5)


@given(p=st.floats(0, 1), f=st.floats(0, 1))
def test_shrink_never_crosses_50(p, f):
    q = float(cal.apply_shrink(p, f))
    assert (q - 0.5) * (p - 0.5) >= 0 and abs(q - 0.5) <= abs(p - 0.5) + 1e-12


def test_calibration_refuses_seasons_after_2022(tmp_path):
    with pytest.raises(cal.CalibrationHoldoutError):
        cal.fit_shrink_factor(np.array([0.6, 0.4]), np.array([1.0, 0.0]), np.array([2022, 2023]))
    bad = json.loads(cal.ARTIFACT.read_text()); bad["training_seasons"] = [2001, 2023]
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    with pytest.raises(cal.CalibrationHoldoutError):
        cal.load_calibration(tmp_path / "bad.json")
    from src.nfl.models.total.fit_calibration import oos_frame
    with pytest.raises(cal.CalibrationHoldoutError):
        oos_frame(last_season=2023)
    with pytest.raises(ValueError):
        cal.apply_shrink(0.6, 1.5)


def test_frozen_artifact():
    meta = cal.load_calibration()
    assert meta["shrink_factor"] == 0.2861 and meta["training_seasons"] == [2001, 2022] and meta["training_rows"] == 5811


def test_existing_snapshots_never_rewritten(fixture, tmp_path):
    pred = tmp_path / "data" / "predictions"; pred.mkdir(parents=True)
    old = pred / "totals_2018_wk499_20180101T000000Z.json"; old.write_text('{"frozen": true}')
    before = old.read_bytes()
    out, _ = _snapshot(tmp_path)
    assert old.read_bytes() == before and out != old
    with pytest.raises(FileExistsError):
        pt._write_new(out, "overwrite attempt")
    assert json.loads(out.read_text())["model_version"] == "ridge8_v1"


def test_writes_only_under_predictions_and_md_is_labeled(fixture, tmp_path):
    before = {p for p in tmp_path.rglob("*")}
    out, snap = _snapshot(tmp_path)
    new = {p for p in tmp_path.rglob("*")} - before
    pred = tmp_path / "data" / "predictions"
    assert all(p == pred or p.parent == pred or p == pred.parent for p in new)
    names = sorted(p.name for p in new if p.is_file())
    assert names == sorted([out.name, out.with_suffix(".md").name, "totals_index.json"])
    md = out.with_suffix(".md").read_text()
    assert "Probabilities shown are CALIBRATED (historical close-based)" in md and "\u2014" not in md
    assert "Calibrated P(Over), close-based" in md
    assert ("Probability calibration was fitted against historical closing-line outcomes. It has not yet been "
            "independently validated for this snapshot's market timing.") in md
    g = snap["games"][0]
    assert f"{100 * g['calibrated_probability']:.1f}%" in md


def test_weekly_workflow_records_guard_unchanged():
    # Phase 4 moved the inline shell guard into src/nfl/ops/records_guard.py (tested in
    # tests/unit/test_phase4_records_guard.py). The protections this test pins are unchanged:
    # both record-writing workflows run the guard, copy only data dirs, and push only to records.
    from src.nfl.ops.records_guard import ALLOWED_PREFIXES, APPEND_ONLY, evaluate
    for name in ("weekly.yml", "pregame.yml"):
        wf = (ROOT / ".github" / "workflows" / name).read_text()
        assert "python code/src/nfl/ops/records_guard.py --repo records" in wf
        assert "cp -a code/data/odds/. records/data/odds/" in wf
        pushes = re.findall(r"git push[^\n]*", wf)
        assert pushes and all(p.strip() == "git push origin HEAD:refs/heads/records" for p in pushes)
    wf = (ROOT / ".github" / "workflows" / "weekly.yml").read_text()
    assert "cp -a code/data/predictions/. records/data/predictions/" in wf
    assert ALLOWED_PREFIXES == ("data/odds/", "data/predictions/", "data/weather/")
    # the .md summary is a brand-new file under data/predictions: admitted when added, and refused as an
    # immutable record if it is ever modified later
    md = "data/predictions/totals_2026_wk07_20261018T131000Z.md"
    assert md not in APPEND_ONLY
    assert evaluate([("A", md)], lambda p: b"", lambda p: b"x") == []
    assert evaluate([("M", md)], lambda p: b"x", lambda p: b"y") != []


# ----------------------------------------------------------------------------- amendment: version guard, timing scope

def test_model_version_match_succeeds():
    from src.nfl.models.total.total_model import MODEL_VERSION
    meta = cal.load_calibration(model_version=MODEL_VERSION)
    assert meta["model_version"] == MODEL_VERSION == "ridge8_v1"


def test_model_version_mismatch_fails_closed(fixture, tmp_path, monkeypatch):
    with pytest.raises(cal.CalibrationModelVersionError):
        cal.load_calibration(model_version="ridge9_v2")
    monkeypatch.setattr(pt, "MODEL_VERSION", "ridge9_v2")      # a future live model with the old artifact
    pred = tmp_path / "data" / "predictions"
    with pytest.raises(cal.CalibrationModelVersionError, match="ridge8_v1_shrink_cal_v1"):
        pt.generate(pred_dir=pred)
    assert not pred.exists() or not any(pred.iterdir())       # nothing written when the guard trips


def test_artifact_without_model_version_is_rejected(tmp_path):
    bad = json.loads(cal.ARTIFACT.read_text()); bad.pop("model_version")
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    with pytest.raises(cal.CalibrationModelVersionError):
        cal.load_calibration(tmp_path / "bad.json")


def test_metadata_states_close_based_fit(fixture, tmp_path):
    meta = cal.load_calibration()
    assert meta["historical_market_timing"] == "close" and "closing" in meta["calibration_target"]
    assert meta["timing_validation_status"].startswith("not yet prospectively validated")
    assert "snapshot line at prediction time" in meta["live_market_timing"]
    _, snap = _snapshot(tmp_path)
    block = snap["probability_calibration"]
    for k in cal.TIMING_SCOPE_FIELDS:
        assert block[k] == meta[k]
    assert block["model_version"] == "ridge8_v1" and block["shrink_factor"] == 0.2861
    bad = dict(meta); bad.pop("timing_validation_status")
    with pytest.raises(ValueError):
        cal.validate(bad)
