# Data-Quality Fix: Relocated-Team Codes (Rams, Chargers, Raiders)

Separate from the Phase 3A feature work. Module: `src/nfl/data/team_aliases.py`.
Regression tests: `tests/unit/test_team_aliases.py`. Impact check: `python run.py alias-check --frozen <Phase 2A folder>`.

## Defect

nflverse play-by-play labels every season with the franchise's current code. nflverse schedules use the code of the day:

| Franchise | Schedule code | Seasons | Play-by-play code |
|---|---|---|---|
| Rams | STL | 1999-2015 | LA |
| Chargers | SD | 1999-2016 | LAC |
| Raiders | OAK | 1999-2019 | LV |

`features/epa_features.add_trailing_epa_features` joins play-by-play to schedules by team code. For the 877 STL, SD and OAK games in 1999-2022 the join finds nothing, so those teams never build EPA history; their side of the EPA matchup stays at the 0.0 default. The 16-game scoring window (`features/rolling_scoring.py`) also restarts at each relocation because it is keyed by code. The defect is silent: no error, no missing value.

## Fix

`normalize_schedule_team_codes()` maps STL to LA, SD to LAC and OAK to LV before any join or team-history lookup. Every other code passes through unchanged, so it is idempotent and safe on any season.

## Scope

- Used by the Phase 3A research code (`phase3a_features.py`, `phase3a_harness.py`).
- **Not applied to `ridge8_v1` or the live weekly path.** `ridge8_v1` keeps its Phase 2A behavior exactly; a regression test asserts its feature path still sees the original codes, and `run.py baseline --compare-to` still reproduces the frozen Phase 2A file with zero differences.
- The live 2026 path is effectively unaffected anyway: current schedules already use LA, LAC and LV, and the trailing windows (10 and 16 games) do not reach back to 2019. The defect matters for historical backtests and for the historical rows the Ridge model is trained on.
- Applying the fix to a live model is a model-version change and needs its own approval.

## Regression tests (7, all passing)

1. STL, SD and OAK resolve to LA, LAC and LV.
2. All 32 current codes are unchanged; normalization is idempotent; missing values pass through.
3. Only team columns change; the input frame is not mutated.
4. The defect is reproduced: an OAK schedule row with LV-labelled play-by-play keeps the default EPA forever.
5. The fix restores EPA history for that team.
6. Real 1999-2022 cache: after normalization, both teams of every game are found in that game's play-by-play; before normalization, none of the 877 alias games match. (Skips if the local cache is absent; reads only seasons before 2023.)
7. The frozen `ridge8_v1` feature path keeps the original codes.

## Impact on the frozen Phase 2A headline results (2015-2022)

Same model, same windows, same unstandardized Ridge, same walk-forward. Only the team codes differ. The as-is run reproduces the frozen Phase 2A file exactly.

| Measure | Frozen Phase 2A | With alias fix | Difference |
|---|---:|---:|---:|
| Games | 2,079 | 2,079 | 0 |
| Win % | 50.87% | 50.78% | -0.09 pts |
| ROI at -110 | -2.85% | -3.03% | -0.18 pts |
| Model MAE | 10.762 | 10.772 | +0.010 |
| Market MAE | 10.546 | 10.546 | 0 |
| Brier (over) | 0.2550 | 0.2551 | +0.0001 |
| Log loss | 0.7038 | 0.7039 | +0.0002 |
| ECE | 0.058 | 0.060 | +0.002 |
| Edge-win slope (p) | +0.0010 (0.97) | +0.0005 (0.99) | ~0 |
| Over-pick share | 50.5% | 50.1% | -0.4 pts |

- Games involving an alias team in 2015-2022: 124. Games whose baseline features change: 170 (opponents' matchup features change too).
- Picks that flip: 50 of 2,079. Average projection change 0.15 points. The largest changes (4.2 to 5.4 points) are early-season games right after a relocation (for example 2020 Week 2 NO at LV, 2016 Week 3 LA at TB), where the as-is model had restarted the team's history from scratch.

**Conclusion: the fix is correct and does not materially change any Phase 2A headline number. The Phase 2A conclusion (no edge-to-win relationship, overconfident probabilities) stands with or without it.** Full numbers: `results/alias_fix_impact.json`.
