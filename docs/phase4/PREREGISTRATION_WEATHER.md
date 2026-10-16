# Phase 4 weather pre-registration (frozen)

- Registered: Oct 15 2026, committed to branch `phase-4-market-clv` BEFORE any Phase 4 weather forecast
  was captured. The commit SHA of this file is the timestamp of the registration.
- Applies to: forecasts captured by the Phase 4 pipeline (Open-Meteo, `best_match`) after this commit,
  2026 regular season. No historical or archived forecasts are used. No observed kickoff weather is used.
- 2023-2025 holdout: not used.
- Changes: none allowed after any 2026 outcome is seen. A change requires a NEW pre-registration with a
  later start date, evaluated only on data captured after that date.

## Population (primary)

- 2026 regular-season games with `roof_type = outdoors` (`configs` stadium table).
- Retractable-roof and dome games are excluded from the primary test (reported descriptively only).
- The game must have a weather capture taken in the same run as an odds snapshot S with
  24 to 120 hours to kickoff (normally the Tue / Thu / Sat / Sun-night scheduled pulls).
- If several snapshots qualify, the EARLIEST qualifying snapshot is used. One observation per game.
- The game must also have a `last_observed_pregame` snapshot R strictly after S and strictly before kickoff.

## Weather measures (from the capture at S only)

- Game window = the forecast hour containing kickoff plus the next 3 hours (4 hourly values).
- `wind_mph_window_mean`: mean of hourly `wind_speed_10m` (mph) over the window.
- `precip_prob_window_max`: max hourly `precipitation_probability` (%) over the window.
- `precip_in_window_sum`: sum of hourly `precipitation` (inches) over the window.
- `snow_in_window_sum`: sum of hourly `snowfall` (inches) over the window.

## Flags (thresholds fixed now)

| Flag | Rule |
|---|---|
| `W15` | `wind_mph_window_mean >= 15` |
| `W20` | `wind_mph_window_mean >= 20` (subset of W15, reported as a tier) |
| `WET` | `precip_prob_window_max >= 60` AND `precip_in_window_sum >= 0.10` |
| `EXTREME` | `W20` OR `snow_in_window_sum >= 1.0` |

No other thresholds will be tested for inference in 2026.

## Hypotheses

**H-W1 (primary).** For W15 games versus non-W15 outdoor games:
- (a) Movement: `canonical_total(R) - canonical_total(S)` is lower (more negative) for W15 games.
- (b) CLV: an UNDER taken at S, `best_available` variant within NY-ACTIONABLE books
  (`configs/phase4_books.yaml`), has mean `line_clv > 0` against `canonical_total(R)`.

**H-W2 (secondary).** The same (a) and (b) for WET games versus non-WET outdoor games.

`EXTREME` and `W20` are descriptive tiers only.

## Tests (fixed now)

- (a): difference in mean movement (flagged minus unflagged), one-sided, permutation test,
  10,000 permutations, seed 20261015, alpha 0.05.
- (b): mean Under line CLV for flagged games, game-level bootstrap 95% CI (10,000 resamples,
  seed 20261015); supported only if the CI lower bound is above 0.
- Secondary display (not inference): the same CLV against Pinnacle where it quoted both S and R,
  and against all observed books.
- `close_quality` of R is reported; the test is also shown restricted to close-quality R as a
  robustness check (not a second test).

## Evaluation timing

- Inference is run once, after all Week 18 games are final.
- Interim reporting before then is limited to counts (how many W15 / WET games have been captured).
  No interim movement or CLV numbers for the flagged groups.
- Results are reported regardless of direction. Expected 2026 sample is roughly 15-35 W15 outdoor
  games, so an inconclusive result is the most likely outcome; the same registration may roll into
  2027 as a new registration with a 2027 start date.

## Model hypothesis H-M1 (descriptive only, no inference)

Mean line CLV for the `ridge8_v1` lean side, by |model edge| bucket fixed now:
`< 1.0`, `1.0-1.99`, `2.0-2.99`, `>= 3.0` points. `ridge8_v1` stays at 0 units; this is a
hypothesis about market movement, not a betting rule.
