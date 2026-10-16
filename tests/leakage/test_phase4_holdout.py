"""Phase 4 guards: 2023-2025 sealed, calibration frozen, planner stays stdlib-only, no betting outputs."""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from src.nfl.market import build as B
from src.nfl.market import phase4_config as C

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_build_rejects_any_holdout_or_pre_2026_game():
    for gid in ("2023_05_KC_BUF", "2024_01_A_B", "2025_18_X_Y", "2022_01_A_B"):
        with pytest.raises(AssertionError):
            B.assert_forward_only([gid])
    B.assert_forward_only(["2026_07_SEA_DEN", None, "event:abc"])


def test_build_drops_pre_2026_schedule_rows_before_use():
    s = pd.DataFrame({"season": [2022, 2023, 2024, 2025, 2026], "game_id": list("abcde")})
    assert list(B.forward_schedules(s).season) == [2026]


def test_phase4_modules_never_import_research_harnesses():
    for p in (ROOT / "src/nfl/market").glob("*.py"):
        text = p.read_text()
        for banned in ("backtest", "totals_harness", "phase3a_harness", "phase3b_harness", "fit_calibration"):
            assert banned not in text, f"{p.name} references {banned}"


CALIBRATION_SHA256 = "00480b2c582227bcffdfc7a23cbd0e705389396d074861bcf94824f84fbda669"   # calibration_v1.json as merged in PR #4 (main b8e89ab)


def test_calibration_frozen():
    raw = (ROOT / "src/nfl/models/total/calibration_v1.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CALIBRATION_SHA256
    cal = json.loads(raw)
    assert cal["shrink_factor"] == pytest.approx(0.2861, abs=5e-5)
    assert cal["model_version"] == "ridge8_v1"
    assert [int(x) for x in cal["training_seasons"]][-1] <= 2022


def test_pregame_gate_and_timing_are_stdlib_only():
    code = ("import sys; sys.path.insert(0, '.'); import src.nfl.market.pregame_gate, src.nfl.market.timing; "
            "bad=[m for m in ('pandas','numpy','yaml','requests','scipy') if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_units_thresholds_or_bets_in_phase4_code():
    for p in list((ROOT / "src/nfl/market").glob("*.py")) + [ROOT / "src/nfl/reporting/ledger.py"]:
        text = p.read_text().lower()
        assert "kelly" not in text and "best bet" not in text
    assert C.WEEKLY_CREDIT_CAP == 30 and C.CREDIT_RESERVE == 60


def test_terminology_no_opener_or_closing_in_phase4_outputs():
    from src.nfl.data.odds_snapshot import QUOTE_FIELDS
    from src.nfl.market.clv import CLV_FIELDS
    from src.nfl.reporting.ledger import FIELDS
    for f in QUOTE_FIELDS + CLV_FIELDS + FIELDS:
        assert "opener" not in f and "opening" not in f and "closing" not in f
    assert "opener" not in C.ALL_KINDS and "current" not in C.ALL_KINDS and "pregame" not in C.ALL_KINDS
