"""Frozen Phase 4 constants (spec docs/phase4/PHASE_4_SPEC.md, approved Oct 15 2026).

Standard library only: the pregame planner imports this without installing the package.
Changing any value here requires John's approval recorded as a spec amendment. The weather
thresholds below are the pre-registered ones (docs/phase4/PREREGISTRATION_WEATHER.md).
"""
from __future__ import annotations

SPEC_VERSION = "phase4_spec_v1_A1"
ET_ZONE = "America/New_York"
FORWARD_SEASON_MIN = 2026          # Phase 4 reads 2026+ only; 2023-2025 stays sealed
HOLDOUT_SEASONS = (2023, 2024, 2025)

# ---- Odds API -------------------------------------------------------------------------------
ODDS_MARKET = "totals"
EXPECTED_CREDITS_PER_PULL = 1      # 1 market x up to 10 named books
CREDIT_RESERVE = 60                # refuse a paid pull when remaining credits are below this
WEEKLY_CREDIT_CAP = 30             # refuse a paid pull after this many pulls in the NFL week (Tue 00:00 ET start)

# ---- Snapshot kinds -------------------------------------------------------------------------
# kind -> (ET weekday Mon=0..Sun=6, hour, minute). The workflow fires one cron for EDT and one
# for EST; the ET gate below keeps only the one that lands at or after the ET target.
SCHEDULED_KINDS = {
    "tue_scheduled": (1, 10, 10),
    "thu_scheduled": (3, 9, 10),
    "sat_scheduled": (5, 9, 10),
    "sun_scheduled": (6, 9, 10),
    "sun_night_scheduled": (6, 23, 30),
}
SCHEDULE_GATE_TOLERANCE_MIN = 120  # legacy constant (pre-incident); no longer used by the gate
# Late-snapshot policy (scheduling recovery, approved Oct 2026): a delayed scheduled run still captures
# until the EARLIER of (target + 18 h) and the NEXT scheduled target of any kind. The capture keeps its
# real timestamp; the intended target and the delay are recorded separately (never backdated).
SCHEDULE_LATE_MAX_MIN = 18 * 60
DELAYED_THRESHOLD_MIN = 15         # captures more than 15 min after target are labeled delayed
TRIGGERS = ("github_cron", "external_dispatch", "manual", "pregame_planner", "validation", "schedule")
OTHER_KINDS = ("pregame_window", "manual", "legacy_current")
ALL_KINDS = tuple(SCHEDULED_KINDS) + OTHER_KINDS
PREDICTION_KINDS = ("thu_scheduled", "sat_scheduled", "sun_scheduled")   # unchanged cadence
SETTLE_KINDS = ("tue_scheduled",)

# ---- Pregame planner ------------------------------------------------------------------------
PREGAME_TARGET_MIN = 20
PREGAME_WINDOW_MIN = 10            # target window [10, 30] minutes before kickoff
PREGAME_WINDOW_MAX = 30
PREGAME_LATE_MIN = 5               # one late attempt allowed down to T-5, never after kickoff
PREGAME_MAX_ATTEMPTS_PER_SLOT = 2
PREGAME_ATTEMPT_LOOKBACK_MIN = 40  # snapshots within 40 min before a kickoff count as attempts for it
# Scheduling recovery: when a scheduled full-slate snapshot is due inside a game's T-30..T-10 window
# (e.g. a 9:30 AM ET international game and sun_scheduled at 9:10), the planner waits up to 8 min after
# that target for it instead of buying a second pull for the same game; if it has not landed, the planner
# pulls as usual (still inside the window, never after kickoff).
PREGAME_SCHEDULED_DEFER_MIN = 8

# ---- Kickoff resolution -----------------------------------------------------------------------
KICKOFF_AGREE_TOL_MIN = 5          # Odds API and nflverse kickoffs within 5 min = agreed (verified)

# ---- Close-quality rule ---------------------------------------------------------------------
CLOSE_QUALITY_MAX_MIN = 30         # last_observed_pregame taken 0-30 min before kickoff (strictly before)
CLOSE_QUALITY_MIN_BOOKS = 5        # and the game's kickoff must be RESOLVED (both sources agree)

# ---- Settlement -------------------------------------------------------------------------------
SETTLE_MIN_HOURS_AFTER_KICKOFF = 5 # a final score is used only once nflverse has it AND kickoff + 5 h has passed

# ---- Monitoring -------------------------------------------------------------------------------
MONITOR_START_UTC = "2026-10-22T00:00:00+00:00"   # alerts only for targets/kickoffs after the recovery deploy
MONITOR_LOOKBACK_DAYS = 8
SETTLE_ALERT_HOURS = 36            # a predicted, final game still unsettled 36 h after kickoff is an alert
RECORDS_STALE_HOURS = 50           # in season, no records commit for 50 h is an alert
ALERT_LABEL = "collection-alert"

# ---- Line shopping / CLV --------------------------------------------------------------------
STALE_QUOTE_MIN = 360
KEY_TOTALS = (37, 41, 43, 44, 47, 51)
SAME_LINE_CONSENSUS_MIN_BOOKS = 3
SHOP_ADV_THRESHOLDS = (0.5, 1.0, 1.5)
EDGE_BUCKETS = ((0.0, 1.0, "<1.0"), (1.0, 2.0, "1.0-1.99"), (2.0, 3.0, "2.0-2.99"), (3.0, float("inf"), ">=3.0"))
DISPERSION_BUCKETS = ((0.0, 0.5, "0"), (0.5, 1.0, "0.5"), (1.0, float("inf"), "1.0+"))

# ---- Weather (pre-registered; do not change after 2026 outcomes are seen) -------------------
WEATHER_PROVIDER = "open-meteo"
WEATHER_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
WEATHER_FORECAST_DAYS = 16          # Open-Meteo maximum; horizon counts from 00:00 GMT of the request day
WEATHER_WINDOW_HOURS = 4           # kickoff hour + next 3 hours
WEATHER_HOURLY_VARS = ("temperature_2m", "relative_humidity_2m", "precipitation_probability", "precipitation",
                       "snowfall", "weather_code", "wind_speed_10m", "wind_gusts_10m")
W15_MPH = 15.0
W20_MPH = 20.0
WET_PROB_PCT = 60.0
WET_PRECIP_IN = 0.10
EXTREME_SNOW_IN = 1.0
WEATHER_PRIMARY_MIN_HOURS = 24
WEATHER_PRIMARY_MAX_HOURS = 120
