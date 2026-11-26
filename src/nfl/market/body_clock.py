"""West Coast body-clock spot: fixed definitions (Phase 5D pre-registration, approved Nov 26 2026).

Pure definitions only, shared by the Phase 5D historical test (it imports this module) and the weekly
sheet-check flag. Nothing here predicts anything. The flag is informational; VERDICT below carries the test
result so the label on the weekly table is honest about what the history showed.

Qualifying game: one Pacific-time team against one Eastern-time team, at either team's home stadium.
Central and Mountain teams never qualify (John's decision: DEN and ARI do not count as West Coast).
Windows are kickoff times in US Eastern time:
  primary   ("night"): 7:00 PM to 10:59 PM ET  -> West Coast body clock 4:00 to 7:59 PM
  secondary ("4pm")  : 4:00 PM to  6:59 PM ET  -> exploratory only; theory does not favor the West team here
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

EASTERN = frozenset({"NE", "NYJ", "NYG", "BUF", "MIA", "PHI", "PIT", "BAL", "WAS", "CLE", "CIN", "DET", "ATL",
                     "CAR", "TB", "JAX", "IND"})
_PACIFIC_ALWAYS = frozenset({"SEA", "SF", "LAC", "SD", "LV", "OAK"})   # incl. the same franchises' older codes
RAMS_LA_FROM = 2016                                                    # "LA" = Rams; St. Louis (STL) is Central
PRIMARY = ("night", 19 * 60, 23 * 60)        # [start, end) minutes after midnight ET
SECONDARY = ("4pm", 16 * 60, 19 * 60)
ET = ZoneInfo("America/New_York")

# Set from the Phase 5D result (docs/phase5/PHASE_5D_WEST_COAST_REPORT.md). Label text only.
VERDICT = {"night": "tested, not supported: 62% covers since 2000, 53% since 2015",
           "4pm": "exploratory, no edge: 46% covers since 2000"}


def is_pacific(team: str, season: int) -> bool:
    return team in _PACIFIC_ALWAYS or (team == "LA" and season >= RAMS_LA_FROM)


def window_for(minutes_et: float | None) -> str | None:
    if minutes_et is None or minutes_et != minutes_et:      # None or NaN
        return None
    for name, lo, hi in (PRIMARY, SECONDARY):
        if lo <= minutes_et < hi:
            return name
    return None


def classify(home_team: str, away_team: str, season: int, minutes_et: float | None,
             neutral: bool = False) -> dict | None:
    """None unless the game is a qualifying West vs East game inside one of the two windows."""
    if neutral:
        return None
    wh, wa = is_pacific(home_team, season), is_pacific(away_team, season)
    if wh and away_team in EASTERN:
        west, west_home = home_team, True
    elif wa and home_team in EASTERN:
        west, west_home = away_team, False
    else:
        return None
    w = window_for(minutes_et)
    if w is None:
        return None
    return {"window": w, "west_team": west, "west_home": west_home}


def flag_text(spot: dict | None) -> str:
    if not spot:
        return ""
    label = "night" if spot["window"] == "night" else "4 PM ET window"
    return f"West Coast body-clock spot ({label}): {spot['west_team']} [{VERDICT[spot['window']]}]"
