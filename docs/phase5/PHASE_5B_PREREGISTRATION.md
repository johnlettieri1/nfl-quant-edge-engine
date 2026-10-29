# Phase 5B pre-registration: can a spread model beat the closing line?

Written Oct 29 2026, BEFORE any Phase 5B model was fit. Nothing below changes after the first run.
Any later change is logged in a "Deviations" section of the report with the reason.

Status: APPROVED by John, Oct 29 2026 ("approve, and ok for #2"). Frozen from this point. Section 10 scoping
(model-pick CLV ledger deferred until a model passes) confirmed.

## 1. Question

Using only free historical data (nflverse schedules and play-by-play), can a walk-forward model predict the
home-minus-away margin better than the closing spread, out of sample, on 2015-2022?

The honest prior: NFL spreads are a very efficient market. The most likely result is "no reliable edge".

## 2. What is fixed and untouched

- ridge8_v1, calibration 0.2861, the frozen bookmaker groups, the weather pre-registration, the totals ledger,
  and every historical record: unchanged.
- 2023-2025 holdout: sealed. The dev loader (`phase3a_harness._load_dev_inputs`) drops every schedule row and
  every play from 2023 on before any feature is computed, and the harness asserts it. No holdout prediction
  is written. Opening the holdout needs John's written approval.
- 0 units. Research only. No paid API calls (the Odds API is not touched; everything comes from the nflverse
  cache built by `python run.py data`).

## 3. Target and convention

- Target: `residual = (home_score - away_score) - spread_line` (actual margin minus the closing spread).
  nflverse `spread_line` is positive when the home team is favored (pinned by
  `tests/leakage/test_spread_sign_convention.py`).
- A model predicts `r_hat`. Its implied margin is `spread_line + r_hat`. The market baseline is `r_hat = 0`.
- Side rule: `r_hat > 0` picks the home side, `r_hat < 0` picks the away side, `r_hat = 0` is no pick.
- Home covers if `residual > 0`; push if `residual = 0` (pushes are excluded from cover rates).

## 4. Windows (same as every prior phase)

| Window | Seasons | Use |
|---|---|---|
| Training start | 1999 (2006 for anything using spread prices) | Expanding window, at least 500 games |
| Replication | 2010-2014 | Sanity only: leakage tests pass, MAE in the market's ballpark. Not used for pass/fail. |
| Development | 2015-2022, regular season | The only window the pass bar is judged on |
| Holdout | 2023-2025 | SEALED |

Walk-forward: for season S, fit on every completed game with season < S, predict all of season S, move on.
Features for a game use only data from games that finished before it (same trailing-window code as Phase 3A/3B).

Market baseline on the dev window, measured Oct 29 2026 from the schedule file (2,079 games): closing spread
MAE 9.85 points, pushes 2.6%, home covers 48.2% of non-push games.

## 5. Candidates (fixed list, no additions after the first run)

All candidates are Ridge regressions on the residual (alpha 10, features standardized on the training fold
only, flat per-season sigma from training residuals), exactly the Phase 3B residual recipe. No tuning.

| id | Features (home minus away unless noted) | Why |
|---|---|---|
| s0_market | none (r_hat = 0) | the baseline every candidate must beat |
| s1_line | `spread_line` only | does the market systematically over- or under-shoot favorites or home teams? |
| s2_elo | Elo diff, rest diff | the simplest team-strength model |
| s3_margin4 | Elo diff, rest diff, trailing net EPA diff, trailing QB EPA diff | the repo's existing margin model features, re-aimed at the residual |
| s4_scoring | s3 + trailing points scored/allowed diffs (ridge8-style, 16-game window, relocated-team fix) | the box-score view |
| s5_form_xpl | s3 + Phase 3A form (half-life weighted points and EPA) and explosive-play rates, differenced | the features that came closest in Phase 3A |
| s6_qb | s3 + Phase 3B QB context (change, backup, first start, EPA delta), signed home minus away | starter changes are the biggest single-week shock |
| s7_inj | s3 + Phase 3B injury groups (QB, OL, WR/TE, RB, front, secondary), differenced; 2013+ only | with `s3_2013`, an identical-window control, so the injury effect is not confused with the shorter window |

Eight model candidates plus the baseline. If a feature cannot be built as described, the candidate is dropped
and the drop is logged; nothing is substituted.

## 6. Pass bar (fixed in advance)

A candidate PASSES only if it clears BOTH bars on the dev window, 2015-2022 regular season, out of sample.

**Bar 1: margin error.** Per game, `|margin - (spread_line + r_hat)| - |margin - spread_line|`. Paired
bootstrap of the mean difference, 10,000 resamples, seed 20261029. The confidence interval must sit entirely
below zero (the model is strictly better than the closing line).

**Bar 2: cover rate.** Pick the side by the sign of `r_hat` on every non-push game with `r_hat != 0`. Wilson
confidence interval on the cover rate. The lower bound must be above 50%.

**Multiple comparisons.** Eight candidates get eight chances to look lucky. The pass decision uses
confidence intervals at 1 - 0.05/8 (about 99.4%). Plain 95% intervals are reported alongside for reading.

**Practical bar, reported but not the pass test.** Breakeven at -110 is 52.4%. The report states whether the
point estimate and the lower bound clear 52.4% too. A candidate that passes the formal bar but not 52.4% is
labeled "statistically better than the close, not yet profitable at -110".

**What is NOT allowed.** No threshold search, no per-season cherry-picking, no re-running with new features,
no touching the holdout. One pre-declared descriptive cut is reported for information only: cover rate when
`|r_hat| >= 1.0` and when `|r_hat| >= 2.0`. These cuts are not used for pass/fail.

**Outcome rules.**
- Nothing passes: spreads stay market tracking only (5A line shopping and movement). Said plainly in the
  report. No spread model goes live, no spread ledger of model picks is built.
- Something passes: it still does NOT go live. Next step would be a separate, John-approved decision on whether
  to open the holdout for a one-shot confirmation. The holdout is opened once or never.

## 7. Descriptive extras (reported, not part of the bar)

- Per-season MAE and cover rate for each candidate (to show whether any result is one season's luck).
- Log loss and Brier of the Normal cover probability against the no-vig market cover probability where spread
  prices exist (2006+).
- Replication-window (2010-2014) MAE and cover rate.
- Calibration table in 5 bins for the best candidate.

## 8. Leakage checks (must pass before the report is written)

- Existing suite: `python run.py test` green.
- New test: shifting every candidate's feature frame by one game changes no earlier prediction (trailing-window
  check), and no row with season >= 2023 exists in any prediction file.
- New test: `spread_line` sign convention still matches the pinned test.

## 9. Deliverables

- `docs/phase5/PHASE_5B_PREREGISTRATION.md` (this file, frozen once approved).
- `src/nfl/backtest/phase5b_harness.py` and `src/nfl/models/spread/residual_spread.py`.
- `docs/phase5/results/phase5b_*.csv` out-of-sample predictions per candidate (dev window only).
- `docs/phase5/PHASE_5B_REPORT.md`: results, pass/fail per candidate, deviations, plain conclusion.
- Pull request from branch `research/phase5b-spread-model` into `main`. Not merged without John's approval.

## 10. Spread CLV ledger (moved to 5B in the plan)

Nothing to grade until a spread model passes. Proposed: defer the model-pick ledger. The market-only pieces
(first-observed vs last-observed pregame spread, line shopping) are already produced by 5A from live
collection and need no model. John to confirm this scoping.
