"""Frozen probability calibration for the live ridge8_v1 totals lean (display/reporting only).

Why: Phase 3B showed ridge8_v1's raw Normal-CDF probability is overconfident (stated 60%+ won
about 53%). A walk-forward shrink toward 50% fixed calibration (ECE 0.058 -> 0.012). This module
applies that shrink with ONE frozen factor so the human-facing number is honest.

What it does NOT do: it never changes the projected total, sigma, edge, lean, or units. Shrink is
P_cal = 0.5 + factor * (P_raw - 0.5) with 0 <= factor <= 1, so P_cal is on the same side of 50% as
P_raw: the lean cannot flip.

Holdout rule: the factor is fit only on ridge8_v1 walk-forward out-of-sample predictions from
seasons <= 2022. Fitting refuses any later season, and loading refuses an artifact that claims one.
The frozen artifact is calibration_v1.json next to this file; the live path only reads it.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

CALIBRATION_MAX_TRAINING_SEASON = 2022          # 2023-2025 holdout: never used for calibration
ARTIFACT = Path(__file__).with_name("calibration_v1.json")
EPS = 1e-6


TIMING_SCOPE_FIELDS = ("calibration_target", "historical_market_timing", "live_market_timing", "timing_validation_status")


class CalibrationHoldoutError(ValueError):
    """Raised when calibration would touch a season after 2022."""


class CalibrationModelVersionError(ValueError):
    """Raised when the calibration artifact was fitted for a different model version than the live one."""


def apply_shrink(p_raw, factor: float):
    if not 0.0 <= factor <= 1.0:
        raise ValueError(f"shrink factor must be in [0, 1], got {factor}")
    return 0.5 + factor * (np.asarray(p_raw, dtype=float) - 0.5)


def fit_shrink_factor(p_raw: np.ndarray, over: np.ndarray, seasons: np.ndarray) -> float:
    """Log-loss-minimizing factor in [0, 1]. Pushes must already be excluded (over in {0, 1})."""
    from scipy.optimize import minimize_scalar

    seasons = np.asarray(seasons)
    if seasons.size == 0 or int(seasons.max()) > CALIBRATION_MAX_TRAINING_SEASON:
        raise CalibrationHoldoutError(f"calibration training data must stop at {CALIBRATION_MAX_TRAINING_SEASON}")
    p, y = np.asarray(p_raw, float), np.asarray(over, float)
    if not np.isin(y, (0.0, 1.0)).all():
        raise ValueError("over must be 0/1 with pushes excluded")

    def ll(lam):
        q = np.clip(0.5 + lam * (p - 0.5), EPS, 1 - EPS)
        return float(-(y * np.log(q) + (1 - y) * np.log(1 - q)).mean())
    return float(minimize_scalar(ll, bounds=(0.0, 1.0), method="bounded").x)


def validate(meta: dict) -> dict:
    first, last = meta["training_seasons"]
    if int(last) > CALIBRATION_MAX_TRAINING_SEASON:
        raise CalibrationHoldoutError(f"calibration artifact trained through {last}; maximum allowed is "
                                      f"{CALIBRATION_MAX_TRAINING_SEASON}")
    if meta["method"] != "shrink_toward_0.5" or not 0.0 <= float(meta["shrink_factor"]) <= 1.0:
        raise ValueError("unsupported calibration artifact")
    if not meta.get("model_version"):
        raise CalibrationModelVersionError("calibration artifact does not declare a model_version")
    missing = [k for k in TIMING_SCOPE_FIELDS if not meta.get(k)]
    if missing:
        raise ValueError(f"calibration artifact is missing timing-scope fields: {missing}")
    return meta


def check_model_version(meta: dict, live_model_version: str) -> dict:
    """Fail closed: a calibration fitted for one model version may never be applied to another."""
    declared = meta.get("model_version")
    if declared != live_model_version:
        raise CalibrationModelVersionError(
            f"calibration {meta.get('calibration_version')!r} was fitted for model_version {declared!r} but the live "
            f"model is {live_model_version!r}; refit and version a new calibration (python run.py calibration-fit) "
            "before generating snapshots")
    return meta


def load_calibration(path: Path = ARTIFACT, model_version: str | None = None) -> dict:
    """Load and validate the frozen artifact. With model_version given (the weekly path always passes the
    live MODEL_VERSION), a mismatch raises CalibrationModelVersionError instead of applying the wrong calibration."""
    meta = validate(json.loads(Path(path).read_text()))
    if model_version is not None:
        check_model_version(meta, model_version)
    return meta


def snapshot_metadata(meta: dict) -> dict:
    """The block recorded in every prediction snapshot."""
    return {"calibration_version": meta["calibration_version"], "model_version": meta["model_version"],
            "method": meta["method"],
            "formula": meta["formula"], "shrink_factor": meta["shrink_factor"],
            "training_seasons": meta["training_seasons"], "training_rows": meta["training_rows"],
            **{k: meta[k] for k in TIMING_SCOPE_FIELDS},
            "applies_to": "display only; projected_total, edge, lean and recommended_units are unchanged",
            "raw_field": "raw_model_probability (== model_over_prob)", "calibrated_field": "calibrated_probability"}
