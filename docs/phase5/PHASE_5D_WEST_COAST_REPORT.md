# Phase 5D report: West Coast body-clock test

Run Nov 26 2026 against the frozen pre-registration (`PHASE_5D_WEST_COAST_PREREGISTRATION.md`, approved by John
the same day). Free nflverse schedule data, seasons 2000-2022, no Odds API credits, 0 units. The 2023-2025
holdout was not opened.

## Bottom line

**It does not pass, by one game. The history is real, and it has faded.**

In night games (8:00 to 8:30 PM ET kickoffs) between a Pacific-time team and an Eastern-time team, the West
Coast team went **42-26-1 against the closing spread (61.8%)** from 2000 to 2022, and 43-26 straight up. The
pass bar needed the low end of the confidence range to sit above 50%. It came in at 49.9%. One more cover
would have cleared it. The bar was set before looking, so this is recorded as a fail, not rounded up.

The more important finding is the trend: **24-10 (70.6%) from 2000 to 2014, then 18-16-1 (52.9%) from 2015 to
2022.** Since 2015 the record is not distinguishable from a coin flip.

## Results: primary window (night games)

69 games. West Coast team vs the closing spread:

| Cut | Record (W-L-P) | Cover rate | 95% range |
|---|---|---:|---|
| **All, 2000-2022** | **42-26-1** | **61.8%** | **49.9% to 72.4%** |
| 2000-2014 | 24-10-0 | 70.6% | 53.8% to 83.2% |
| 2015-2022 | 18-16-1 | 52.9% | 36.7% to 68.5% |
| West team at home | 26-18-0 | 59.1% | 44.4% to 72.3% |
| West team away | 16-8-1 | 66.7% | 46.7% to 82.0% |

| Pass bar | Needed | Result | |
|---|---|---|---|
| 1. Low end of the 95% range above 50% | above 50.0% | 49.9% | **FAIL** (43 covers needed, got 42) |
| 2. Above 50% in both halves | both | 70.6% and 52.9% | pass |
| 3. Above 50% home and away | both | 59.1% and 66.7% | pass |

All three were required. **Result: FAIL.**

Other facts, reported but not part of the bar:
- Straight up: 43-26. The West Coast team was favored by 1.5 points on average and won by 4.4 on average.
- 61.8% is above the 52.4% breakeven at -110, but the low end of the range is not.
- By decade: 2000s 14-6, 2010s 20-13-1, 2020-2022 8-7.
- By night: Sunday 26-11, Monday 12-8, Thursday 2-3, Saturday 2-4.

## Results: secondary window (kickoffs 4:00 to 6:59 PM ET), exploratory

297 games. West Coast team: **133-155-9 against the spread (46.2%, range 40.5% to 52.0%)**, 164-133 straight
up. No West Coast edge here. If anything it leans slightly the other way, which is the direction the
body-clock theory predicts (the East Coast team is the one in its late-afternoon window), but the range
includes 50%, so it is not evidence of anything. This window could not pass and is not a claim.

## How to read this

- **John's premise holds for history.** West Coast teams in night games against East Coast teams won and
  covered well above average for 23 seasons. This is the strongest pattern the project has tested: every
  Phase 5B model sat at 48% to 51%.
- **It was strongest early and has shrunk.** 71% before 2015, 53% since. The likely explanation is that the
  line now accounts for it, though this test cannot prove why. A pattern that was 14-6 in the 2000s and is
  8-7 in the 2020s is not one to lean on today.
- **The sample is small.** 69 games in 23 seasons is about three a year. With 34 non-push games since 2015,
  the data cannot tell a 53% edge from a 60% one, or from 45%.
- **The 4 PM ET reading does not help the West Coast team.** Only the night-game version shows anything.

## What was built

- `src/nfl/market/body_clock.py`: the fixed team lists and windows.
- `src/nfl/backtest/phase5d_west_coast.py`: the test (`python -m src.nfl.backtest.phase5d_west_coast`).
- `docs/phase5/results/phase5d_west_coast_games.csv` (every qualifying game with its result) and
  `phase5d_west_coast_summary.json`.
- **Sheet flag.** `sheet-check` now marks qualifying games in the Flags column, for example
  "West Coast body-clock spot (night): LV [tested, not supported: 62% covers since 2000, 53% since 2015]".
  It is informational only: it never changes a pick, an edge or a confidence label (tested). New weekly cards
  record it in a `body_clock_spot` column, so 2026 and later games are tracked forward in the results log.
- `tests/unit/test_body_clock.py` (6 tests): team lists by season, window edges, Mountain and neutral-site
  exclusions, cover logic including pushes, no 2023+ rows, and the flag changing nothing else on the row.
  Full suite: 418 passed, 1 skipped.

## Deviations from the pre-registration

1. The flag label adds the two headline numbers after "tested, not supported", so the weekly table shows how
   close it was and that it has faded. The verdict wording itself is the pre-registered one.
2. New weekly cards gain one column (`body_clock_spot`) for forward tracking. Existing cards and the ledger are
   unchanged.

No definition, window, team list or bar was changed after the run. No extra cuts were searched.

## What happens next (John's call)

- Per the pre-registration, a failed test means the flag is informational only, or removed. Recommended: keep
  it. It costs nothing, it is labeled honestly, and it builds a forward record on real 2026+ games.
- The 2023-2025 holdout stays sealed. The pre-registration reserved a one-shot holdout check for a pass.
- Units stay 0. Nothing changes on the totals side.

Not betting advice.
