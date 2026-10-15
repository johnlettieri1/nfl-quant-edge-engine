"""Phase 3B frozen configuration. See docs/phase3b/PHASE_3B_SPEC.md.

Fixed before any Phase 3B model was evaluated against outcomes. Changing a value is an
amendment that must be logged in the spec.
"""
from __future__ import annotations

# Periods (permanent)
DEV = (2015, 2022)
HOLDOUT_START = 2023
FIRST_SEASON = 1999
RESEARCH_LAST_SEASON = 2022          # no research pull may go past this

# Walk-forward
MIN_TRAIN_GAMES = 500
ALPHA_GRID = (1.0, 10.0, 100.0, 1_000.0, 10_000.0, 100_000.0)
ALPHA_DEFAULT = 1_000.0
INNER_FOLDS = 5

# Calibration
CAL_MIN_ROWS = 500
ISOTONIC_CLIP = (0.02, 0.98)
CAL_SIMPLICITY_ORDER = ("shrink", "logistic", "isotonic")
CAL_LOGLOSS_TOLERANCE = 0.001

# Bootstrap
SEED = 20261015
BOOT_REPS = 4000
BOOT_REPS_SLOPE = 1000

# Weather (section 5.1)
WIND_CAP = 40.0
WIND_STRONG = 15.0
WIND_EXTREME = 20.0
TEMP_EXTREME = 20.0
TEMP_COLD_REF = 40.0
PRECIP_CHANCE_IGNORE_BELOW = 50
OUTDOOR_ROOFS = ("outdoors", "open")

# Injuries (section 5.2)
INJURY_FIRST_SEASON = 2013            # Amendment A1: nflverse snap counts actually start in 2013 (2012 was the spec value)
STATUS_WEIGHT = {"Out": 1.0, "Doubtful": 0.75, "Questionable": 0.25, "Probable": 0.0}
SNAP_WINDOW_GAMES = 4
POS_GROUPS = {
    "qb": ("QB",),
    "ol": ("T", "G", "C", "OT", "OG", "OL"),
    "wrte": ("WR", "TE"),
    "rb": ("RB", "FB", "HB"),
    "front": ("DE", "DT", "NT", "DL", "LB", "OLB", "ILB", "MLB", "EDGE"),
    "sec": ("CB", "S", "FS", "SS", "DB", "SAF"),
}
OFFENSE_GROUPS = ("qb", "ol", "wrte", "rb")

# Quarterback (section 5.3)
QB_BACKUP_LOOKBACK = 8
QB_BACKUP_MIN_STARTS = 4
QB_EPA_WINDOW = 10                    # same window as ridge8_v1's QB feature

WEATHER_FEATURES = ["wx_wind", "wx_wind15", "wx_extreme", "wx_cold", "wx_precip", "wx_snow", "wx_missing"]
INJURY_FEATURES = ["inj_qb", "inj_ol", "inj_wrte", "inj_rb", "inj_front", "inj_sec"]
QB_FEATURES = ["qb_change", "qb_backup", "qb_first_start", "qb_delta"]
