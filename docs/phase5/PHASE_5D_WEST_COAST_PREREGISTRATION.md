# Phase 5D pre-registration: West Coast body-clock test

Written Nov 26 2026, BEFORE any result for this test was looked at. Only game counts were checked.
Nothing below changes after the first run. Any later change is logged as a deviation in the report.

Status: APPROVED by John, Nov 26 2026 ("Yes approve"). Frozen from this point.

Requested by John Nov 26 2026. His decisions: proceed with a pre-registered test; night games (8 PM ET) are
the primary window; Mountain-time teams do not count as West Coast.

## 1. The idea

Players tend to peak in the late afternoon by their body clock. In a night game (about 8:15 PM ET), a West
Coast team's body clock reads about 5:15 PM, while an East Coast team is past its peak. The claim: in those
games the advantage goes to the West Coast team.

## 2. The question we can actually bet on

Not "do West Coast teams win more?" but "do they beat the closing spread?" If the effect is real and known, the
line already includes it. The test is against the closing spread.

## 3. Definitions (fixed)

- **West Coast (Pacific time) teams:** SEA, SF, LAC (SD before 2017), LV (OAK before 2020), LA Rams from 2016
  (St. Louis seasons excluded: Central time).
- **East Coast (Eastern time) teams:** NE, NYJ, NYG, BUF, MIA, PHI, PIT, BAL, WAS, CLE, CIN, DET, ATL, CAR, TB,
  JAX, IND.
- **Excluded:** Central and Mountain teams (DEN, ARI and the rest), neutral-site and international games,
  playoffs, games with no recorded kickoff time (the 1999 season), games with no closing spread.
- **Qualifying game:** one West Coast team against one East Coast team, regular season, played at either
  team's home stadium.
- **PRIMARY window:** kickoff 7:00 PM to 10:59 PM ET (West Coast body clock 4:00 to 7:59 PM). In the data
  every such game kicked off between 8:00 and 8:30 PM ET.
- **SECONDARY window (John's other reading):** kickoff 4:00 PM to 6:59 PM ET. Kept separate so no game is
  counted twice. Under the body-clock theory this window should NOT favor the West Coast team (its body clock
  reads about 1:25 PM), so it is reported for information only.
- **Outcome:** the West Coast team covers the closing spread (nflverse `spread_line`). Pushes are excluded
  from cover rates.

Game counts, 2000-2022 (counted Nov 26 2026, results not viewed):

| Window | Games | 2000-2014 | 2015-2022 | West team at home |
|---|---:|---:|---:|---:|
| Primary (night) | 69 | 34 | 35 | 44 |
| Secondary (4:00 to 6:59 PM ET) | 297 | 180 | 117 | 254 |

## 4. Data and guardrails

- Free nflverse schedule data only. No Odds API credits. 0 units.
- Seasons 2000-2022. The factor has no fitted parameters, so the full range is used as evidence.
- **2023-2025 holdout stays sealed.** The script drops every 2023+ row before computing anything and asserts
  it. 2026 games are not scored here (they are tracked forward by the sheet flag, section 7).
- ridge8_v1, calibration 0.2861, bookmaker groups, weather pre-registration, ledgers and records: untouched.
  This is a spreads-side test. Nothing changes on the totals side.

## 5. Pass bar (fixed in advance; all three required, primary window only)

1. **Cover rate:** West Coast team's cover rate in primary-window games, 2000-2022, with a Wilson 95%
   confidence interval. The lower bound must be above 50%. With 69 games and no pushes that takes about 43
   covers (62%).
2. **It persists:** the cover rate is above 50% in BOTH halves, 2000-2014 and 2015-2022. (Phase 5B showed a
   pattern from the early years can reverse later.)
3. **Not just home field in disguise:** the cover rate is above 50% both when the West Coast team is home and
   when it is away.

Reported next to it, not part of the bar: whether the rate clears the 52.4% breakeven at -110.

**The secondary window cannot pass anything.** It is reported with its own 95% interval and labeled
exploratory. If it shows something interesting, the only next step is a fresh forward test, not a claim.

## 6. Also reported (descriptive only)

- Straight-up win record of the West Coast team in each window, with the average closing spread, since the
  original claim was about winning.
- Cover rate by decade and by day slot (Thursday, Sunday, Monday night).
- The reverse case for context: East Coast teams in 1:00 PM ET games played on the West Coast are not part of
  this test and are not examined.

No other cuts. No searching for a sub-group that works.

## 7. What gets built

- `src/nfl/backtest/phase5d_west_coast.py`: the test, one command, writes the game list and summary to
  `docs/phase5/results/`.
- **Sheet flag:** `sheet-check` marks each qualifying game on the weekly table as
  "West Coast body-clock spot (night)" or "(4 PM ET window)". The label carries the test verdict:
  "tested, passed" or "tested, not supported". The flag never changes a confidence label or a pick by itself.
- Leakage and definition tests (team lists by season, window edges, neutral-site exclusion, no 2023+ rows).
- `docs/phase5/PHASE_5D_WEST_COAST_REPORT.md` and one PR into main. Not merged without John's approval.

## 8. Outcome rules

- **Fails:** said plainly. The flag stays as an informational note labeled "tested, not supported", or is
  removed. John's call.
- **Passes:** the flag is labeled "tested, passed (2000-2022)". Units stay 0. A one-shot confirmation on the
  sealed 2023-2025 seasons would be a separate decision for John; the holdout is opened once or never.
