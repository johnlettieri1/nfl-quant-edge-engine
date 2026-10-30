# Phase 5B report: spread model vs the closing line

Run Oct 29 2026 against the frozen pre-registration (`PHASE_5B_PREREGISTRATION.md`, approved by John the same
day). Free historical data only (nflverse), no Odds API credits, 0 units. Holdout 2023-2025 never opened.

## Bottom line

**No candidate passes. Spreads stay market tracking only.**

All eight pre-registered candidates were worse than the closing spread on margin error (by 0.01 to 0.07 points
of MAE) and none had a cover rate whose confidence interval excluded 50%. The closing line is the better
forecast of the margin, on this data, every way we looked at it. This is the result the plan called the most
likely honest outcome.

## Results, dev window 2015-2022 regular season (2,079 games, out of sample)

Market baseline on these games: closing spread MAE **9.847**, home covers 48.2% of non-push games.

| Candidate | Model MAE | MAE diff vs market (99.4% CI) | Bar 1 | Picks | Cover rate | Cover 99.4% CI | Bar 2 | Result |
|---|---:|---:|---|---:|---:|---|---|---|
| s1_line | 9.862 | +0.015 (-0.003, +0.032) | fail | 2,025 | 47.6% | 44.6%, 50.6% | fail | **FAIL** |
| s2_elo | 9.867 | +0.020 (-0.015, +0.054) | fail | 2,025 | 49.7% | 46.7%, 52.8% | fail | **FAIL** |
| s3_margin4 | 9.867 | +0.019 (-0.017, +0.058) | fail | 2,025 | 49.9% | 46.9%, 53.0% | fail | **FAIL** |
| s4_scoring | 9.862 | +0.015 (-0.028, +0.059) | fail | 2,025 | 51.3% | 48.2%, 54.3% | fail | **FAIL** |
| s5_form_xpl | 9.857 | +0.010 (-0.039, +0.061) | fail | 2,025 | 51.0% | 47.9%, 54.0% | fail | **FAIL** |
| s6_qb | 9.862 | +0.015 (-0.032, +0.065) | fail | 2,025 | 49.3% | 46.3%, 52.3% | fail | **FAIL** |
| s3_2013 (control) | 9.896 | +0.049 (-0.019, +0.115) | fail | 2,025 | 51.0% | 47.9%, 54.0% | fail | **FAIL** |
| s7_inj | 9.915 | +0.067 (-0.006, +0.139) | fail | 2,025 | 50.5% | 47.5%, 53.5% | fail | **FAIL** |

Bar 1 needs the whole interval below zero (model strictly better). Every point estimate is above zero: every
model is slightly worse than the market. Bar 2 needs the lower bound above 50%. The best cover rate, 51.3%
(s4_scoring), has a plain 95% interval of 49.1% to 53.4%; nothing clears 50%, let alone the 52.4% breakeven.
Full numbers: `results/phase5b_summary_dev_2015_2022.csv` and `results/phase5b_results.json`.

## What the extras say (reported, not part of the bar)

- **Per season.** No candidate was consistent. s4_scoring covered 53-56% in 2015-2017 and 48-50% in 2019-2022.
  s5_form_xpl covered 55% in 2021 and 46% in 2022. These are the swings you get from coin flips in 250-game
  seasons.
- **Bigger model disagreements are not better.** On games where a model disagreed with the line by 1+ points,
  cover rates ran 49% to 55% on small samples (169 to 768 picks) with 95% intervals that all include 50%.
  At 2+ points the samples are tiny (7 to 225 picks); one (s5_form_xpl, 44 picks, 59%) looks good but its
  interval runs from 44% to 72%, which is noise. The models rarely disagree with the
  closing line by much: the average absolute predicted residual is 0.2 to 1.0 points.
- **Probability quality.** Every model's log loss on P(home covers) was worse than the no-vig market price
  (0.6933 to 0.6961 vs 0.6927). The 5-bin calibration table for s4_scoring is flat: the top bin (model says
  53% home cover) covered 50%.
- **s1_line is a small lesson.** Fit on 1999-2014, it learned that bigger home favorites had beaten the number
  (positive coefficient on `spread_line`). In 2015-2022 that pattern reversed and the candidate was the worst
  of the eight (47.6%). A historical bias in the line is not a bias you can bet on later.
- **Injuries made it worse, not better.** s7_inj had the largest MAE penalty (+0.067). Its identical-window
  control (s3_2013) was also worse than the market, so part of that is just the shorter training window, but
  the injury features added nothing on top.
- **Replication 2010-2014** (1,280 games): the same picture. Market MAE 10.676, models 10.685 to 10.720, cover
  rates 48.8% to 50.2%.

## Why this is believable rather than a bug

- The walk-forward, the trailing-window builders and the holdout loader are the ones leakage-tested in Phases
  2A, 3A and 3B. New tests (`tests/leakage/test_phase5b_leakage.py`, 7 tests) prove future games cannot change
  earlier predictions, a season's own outcomes cannot change its own predictions, the target is exactly margin
  minus closing spread with the pinned sign convention, the walk-forward refuses 2023+ rows, and the written
  result files contain no holdout season. Full suite: 404 passed, 1 skipped.
- A leaky model would look too good. These look exactly like an efficient market: every model is within a
  tenth of a point of the line and no cover rate is distinguishable from 50%.
- The original pre-fork repo's own walk-forward spread backtest (README, 2010-2025) reported the same thing:
  model margin MAE 10.33 vs market 10.08, ATS accuracy about 51%, "no ATS edge".

## Deviations from the pre-registration

1. Section 5 says "exactly the Phase 3B residual recipe" and also "Ridge alpha 10, no tuning". Phase 3B chose
   alpha from an inner-fold grid. The explicit statement (alpha 10 fixed, no tuning) was followed. Alpha
   selection could only have helped or hurt by a hair; none of the intervals are close to passing.
2. The replication window has no predictions for the two 2013+ candidates (s3_2013, s7_inj): 500 training games
   need two prior seasons, so their first predicted season is 2015. Recorded as n=0, no substitution.
3. The pre-registration's section 8 named a "shift the feature frame by one game" test; it was implemented as
   the stronger future-invariance and own-season-invariance tests above (corrupt everything after a cutoff and
   check earlier predictions are bit-identical), matching the Phase 3B test style.

No candidate was added, removed, re-run with different features, or evaluated on a cut not listed in advance.

## What happens next

- Spreads: 5A keeps collecting paired quotes, line shopping and first/last pregame movement. No spread model,
  no model-pick CLV ledger (deferred, confirmed by John), no spread picks from the engine.
- 5C (weekly spread sheet support) stays labeled as line shopping plus injury checks, as the plan says.
- If John wants another attempt later, it needs a new pre-registration with genuinely new information, not more
  public box-score and play-by-play stats. The eight candidates here covered team strength, recent form,
  explosives, QB changes and injuries, and none of it beat the number.
- The holdout stays sealed. Nothing here earns the right to open it.

## Files

- `docs/phase5/PHASE_5B_PREREGISTRATION.md` (frozen plan)
- `src/nfl/models/spread/residual_spread.py` (frame, signed QB and injury builders, walk-forward)
- `src/nfl/backtest/phase5b_harness.py` (run, pass bar, extras; `python -m src.nfl.backtest.phase5b_harness`)
- `docs/phase5/results/` (per-candidate out-of-sample predictions, dev and replication; summary CSV; results,
  fit logs and run metadata JSON)
- `tests/leakage/test_phase5b_leakage.py`

Not betting advice. Recommended units remain 0.
