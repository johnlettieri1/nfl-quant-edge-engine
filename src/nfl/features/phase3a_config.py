"""Phase 3A frozen configuration. See docs/phase3a/PHASE_3A_FEATURE_SPEC.md.

These values were fixed before any Phase 3A candidate was evaluated against outcomes.
Changing any of them is an amendment that must be recorded in the spec.
"""
from __future__ import annotations

# EW estimator (section 1)
HALF_LIFE_PRIMARY = 6.0
HALF_LIFE_SENSITIVITY = 12.0
OFFSEASON_CARRYOVER = 0.5
PRIOR_STRENGTH_GAMES = 4.0

# Neutral situation (section 2)
NEUTRAL_WP_LOW = 0.20
NEUTRAL_WP_HIGH = 0.80
NEUTRAL_MIN_HALF_SECONDS = 120
NEUTRAL_DOWNS = (1, 2, 3)
NEUTRAL_MAX_QTR = 4

# Pace (section 3)
SPP_GAP_MIN_EXCLUSIVE = 0.0
SPP_GAP_MAX_INCLUSIVE = 50.0
SPP_MIN_GAPS = 5

# Reliability (section 3)
RELIABLE_FROM = {"nohuddle": 2006, "proe": 2006, "def_proe_faced": 2006}

# Explosives (section 3)
EXPLOSIVE_PASS_YARDS = 20
EXPLOSIVE_RUSH_YARDS = 10

# Possessions (section 4)
REGULATION_SECONDS = 3600.0

# Fixed neutral defaults (section 6), used only when the prior season has no observations
DEFAULTS = {
    "off_pts": 22.0, "def_pts": 22.0,
    "off_epa": 0.0, "def_epa": 0.0, "off_pass_epa": 0.0, "off_rush_epa": 0.0,
    "off_sr": 0.45, "def_sr": 0.45,
    "spp": 28.0, "off_plays": 63.0, "def_plays": 63.0, "nohuddle": 0.04,
    "drives": 11.5, "plays_per_drive": 5.5, "neutral_pass": 0.55, "proe": 0.0, "def_proe_faced": 0.0,
    "xpl_pass": 0.08, "xpl_rush": 0.10, "xpl_pass_allowed": 0.08, "xpl_rush_allowed": 0.10,
    "drive_secs": 160.0, "drive_secs_faced": 160.0,
}

GROUP_A = ["form_pts_home", "form_pts_away", "form_epa_home", "form_epa_away",
           "form_pass_epa_sum", "form_rush_epa_sum", "form_sr_home", "form_sr_away"]
GROUP_B = ["pace_spp_sum", "pace_off_plays_sum", "pace_def_plays_sum", "pace_nohuddle_sum", "pace_drives_sum",
           "pace_ppd_sum", "proe_sum", "neutral_pass_sum", "def_proe_faced_sum"]
GROUP_C = ["xpl_pass_home", "xpl_pass_away", "xpl_rush_home", "xpl_rush_away"]
GROUP_D = ["exp_game_possessions"]
GROUPS = {"form": GROUP_A, "pace": GROUP_B, "xpl": GROUP_C, "poss": GROUP_D}

# Ridge settings shared with ridge8_v1
RIDGE_ALPHA = 10.0
MIN_TRAIN_GAMES = 500
