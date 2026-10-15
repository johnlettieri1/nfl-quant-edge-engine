# Phase 3B Specification: Market-Residual Model and Probability Calibration (FROZEN before evaluation)

Status: **frozen**. Written and committed before any Phase 3B model was evaluated against
game outcomes. Choices below come from the Phase 3B brief, the Phase 3A findings, and
data-coverage checks only (which seasons have a column populated). No definition, window,
threshold, weight, grid or rule was chosen by looking at residual or betting results.

Frozen values live in `src/nfl/models/residual/phase3b_config.py`. The commit that adds this
file and that config is the freeze point. Any later change is an amendment, logged at the
bottom with its reason, and affected models are reported both ways.

---

## 0. Periods (unchanged, permanent)

| Period | Seasons | Use in Phase 3B |
|---|---|---|
| Training history | 1999-2014 | Fitting, inner validation, calibration training only |
| Development | 2015-2022 regular season with a closing total | All evaluation and every decision |
| Holdout | 2023-2025 | **Not loaded.** Play-by-play, injury, snap-count and weather pulls stop at 2022. Schedule rows >= 2023 are dropped at load and asserted. No prediction, calibration fit, or feature value for a 2023+ game is ever produced |
| Forward test | 2026 Weeks 7-18 | Prospective only. Not part of this phase |

## 1. Target

```
residual            = total_points - total_line          (actual final total minus closing total)
predicted_total     = total_line + predicted_residual
edge (points)       = predicted_residual
pick                = OVER if predicted_residual > 0, UNDER if < 0, none if exactly 0
```

`total_line` is the nflverse schedules `total_line`, the same closing total every earlier phase used.
Pushes (residual = 0) stay in the regression target and are excluded from win, Brier and log-loss.

**Close-based research benchmark.** Every historical number in Phase 3B is measured against the
closing total. It is NOT evidence that the same advantage existed at an earlier betting time. No
historical opener is used or manufactured. The 2026 opener/current snapshots on the `records`
branch remain the only timing and CLV dataset.

Identity worth stating once: residual MAE and final-total MAE are the same number,
because |actual - (line + r)| = |residual - r|. Market MAE = mean |residual| (the zero-residual model).

## 2. Walk-forward fitting (all residual models)

- For test season S: fit on every completed game with season < S and a closing total
  (all game types, expanding from 1999, at least 500 training games), predict season S.
- Features standardized on the training fold only. Ridge, unpenalized intercept.
- **Ridge alpha chosen inside the walk-forward, never on the test season.** Grid
  {1, 10, 100, 1,000, 10,000, 100,000}. Inner folds = the up-to-5 seasons immediately before S
  that themselves have at least 500 earlier training games; each inner fold is fit on seasons
  before it. Alpha = lowest pooled inner residual MSE. If no inner fold exists, alpha = 1,000.
  Large alpha shrinks toward the market (predicted residual 0), which is the intended prior.
- Group models whose inputs start after 1999 (injuries) train only on seasons where the inputs
  exist, and are compared with a control fitted on the identical window.

## 3. Candidate models (3B-1)

| ID | Inputs | Role |
|---|---|---|
| `m0_market` | none; predicted residual = 0; P(over) = no-vig closing probability | Market baseline (serious competitor) |
| `r0_line` | closing total only | Is the close biased by its own level? |
| `r1_ridge8` | closing total + the 8 ridge8_v1 features (relocated-team codes fixed) | Residual model on ridge8_v1 information |
| `r1o_ridge8_origcodes` | same, original team codes | Control for the code fix |
| `r1_form`, `r1_pace`, `r1_xpl`, `r1_poss` | r1 + one Phase 3A group (h = 6) | Ablation |
| `r2_phase3a` | r1 + all 22 Phase 3A features | Residual model on Phase 3A information |
| `r3_conservative` | r1 + only groups selected inside the walk-forward (rule below) | Conservative model |

**r3 selection rule (nested, no look-ahead):** for test season S, a Phase 3A group is included
if, on the inner folds of section 2 (seasons before S only), r1 + group has lower pooled residual
MSE than r1. Selection is redone each season; the selected groups are logged per season.

**Preserved references** (evaluated by the same code):
- `ridge8_v1` (frozen Phase 2A predictions, reproduced bit-for-bit), edge = model total - line
- `p3a_c3_xpl`, the best Phase 3A candidate (failed its keep rule)
- `m0_market`

No boosted trees in this phase unless a linear residual model passes the keep rule (section 7).

## 4. Probability layer (3B-2)

Two probability views, reported separately:

- **Raw (reference only):** P(over) = Phi(r / sigma), sigma = training residual SD. This is the old
  flat Normal-CDF mapping and is shown only to measure what calibration fixes.
- **Calibrated (primary):** a calibrator maps r to P(over), trained only on out-of-sample
  predictions from seasons before S (every OOS prediction from the first OOS season through S - 1,
  pushes excluded, at least 500 rows; else P = 0.5). No observation calibrates itself, and no
  calibrator ever sees its own test season.

Calibrators (all three are run on every serious candidate):
1. `logistic` (primary): logit P(over) = a + b * r.
2. `shrink`: P = 0.5 + lambda * (Phi(r / sigma) - 0.5), lambda in [0, 1] by min log loss.
3. `isotonic`: monotone map r -> P(over), clipped to [0.02, 0.98].

Choice rule: the simplest method (shrink < logistic < isotonic) whose development log loss is
within 0.001 of the best of the three. The ridge8_v1 and c3 edges also go through the same
calibrators, as a control ("does calibration alone fix the old model?").

Calibrated pick = OVER if P(over) > 0.5. Disagreements with the sign of r are counted and reported.

## 5. Context groups (3B-3), tested only after 3B-1/3B-2 are evaluated

Each group is added to r1 (primary) and to r0 (sensitivity), separately, then all three together
(exploratory only). Timing labels are mandatory in every table.

### 5.1 Weather (W): OBSERVED historical, not forecasts
Source: nflverse schedules `temp`, `wind`, `roof`; missing temp/wind filled from the play-by-play
game `weather` text; still missing -> training median plus a missing flag.
- `outdoor` = roof in {outdoors, open}. Indoor games get 0 on every weather feature.
- `wx_wind` = sustained wind (mph), capped at 40 (values above are data errors).
- `wx_wind15` = wind >= 15. `wx_extreme` = wind >= 20 or temp <= 20 or snow.
- `wx_cold` = max(0, 40 - temp) / 10.
- `wx_precip` = rain/snow/showers/drizzle/sleet/flurries/storm in the weather text after removing
  "N% chance of ..." phrases with N < 50. `wx_snow` = the snow subset.
- Gusts: **DATA UNAVAILABLE** historically.
- Timing: these are kickoff observations. They are closer to a late forecast than to Tuesday
  information and overstate what was knowable at any earlier betting time.

### 5.2 Injuries (I): final pregame report, 2012+
Source: nflverse injuries (`report_status`, `date_modified`) and PFR snap counts.
- A report row is used only if `date_modified` is earlier than kickoff (UTC); later rows are dropped
  and counted.
- Status weight: Out 1.0, Doubtful 0.75, Questionable 0.25, Probable / blank 0.
- Importance = player's mean snap share (offense or defense) over his team's previous 4 games,
  0 for games he missed, using only games completed before this one. ID join gsis -> pfr via the
  nflverse player table.
- Six features, each the home + away sum of status weight x importance:
  `inj_qb`, `inj_ol` (T/G/C), `inj_wrte`, `inj_rb`, `inj_front` (DE/DT/NT/LB), `inj_sec` (CB/S).
- Training window 2012+; compared with r1 fitted on 2012+ only.
- Timing: the final report posts about two days before kickoff (Friday for Sunday games). Its
  `date_modified` proves it existed before kickoff, not before an earlier betting time.

### 5.3 Quarterback (Q)
Source: schedules `home_qb_id` / `away_qb_id` (the actual starter) and play-by-play QB EPA.
- `qb_change` = starter differs from the team's previous-game starter (home + away count).
- `qb_backup` = starter started fewer than 4 of the team's previous 8 games (team must have 4+).
- `qb_first_start` = starter has no earlier start in the data.
- `qb_delta` = starter's trailing QB EPA/dropback minus the previous-game starter's trailing QB
  EPA/dropback, both as of this game (home + away).
- Timing: the starter column is filled after the fact. The real starter is normally public by the
  inactive list (90 minutes before kickoff), so it was known before the close, but not necessarily
  before an earlier betting time. Live use must use the announced or projected starter.

## 6. Metrics (every serious candidate)

Residual layer (pick = sign of r): residual MAE (= final-total MAE), market MAE, OOS R^2 on the
residual (1 - SSE / sum residual^2), signed bias mean(residual - r), Pearson and Spearman of r vs
residual with bootstrap CI, OLS slope of residual on r with CI and p, directional accuracy,
win % and ROI at -110, logistic slope of win on |r| with Wald CI, bootstrap CI and p,
frozen Phase 2A edge buckets with ROI, Over/Under split, by season, leave-one-season-out.

Probability layer (calibrated P): Brier, log loss, ECE (Phase 3A definition on p_pick bins
50-52 ... 60+) and 10-bin ECE on P(over), calibration tables, logistic slope of win on
(p_pick - 0.5), paired bootstrap Brier and log-loss differences vs ridge8_v1 and vs the market.

Bootstrap: game-level, 4,000 reps (1,000 for slopes), seed 20261015.

## 7. Pre-registered keep rule

A Phase 3B model advances only if **all** pass on 2015-2022:

1. **Residual signal:** Pearson(r, residual) > 0 with p < 0.05, and OOS R^2 > 0
   (it predicts the market's error better than "the market is right").
2. **Edge vs success:** logistic slope of win on |r| > 0 with p < 0.05.
3. **Probability quality:** calibrated Brier and log loss below ridge8_v1 (0.2550 / 0.7038), and
   calibrated Brier below the no-vig market probability.
4. **Calibration:** Phase 3A-definition ECE at most half of ridge8_v1's (<= 0.029), and 10-bin
   P(over) ECE below ridge8_v1's.
5. **Not one season or one side:** win slope on |r| positive for Over picks and for Under picks,
   and Pearson(r, residual) positive in at least 5 of 8 seasons.
6. **Leave-one-season-out:** Pearson(r, residual) and the win slope stay positive with each season left out.

Significance is evidence, not a target. Nothing is re-tuned after this freeze. No betting threshold
is examined or optimized. Kelly, units, confidence scores and "best bets" are not built.

If no model passes, the report says so, and recommends whether to pivot to market timing, line
shopping, opener-to-close movement and CLV prediction.

---

## Amendments

(none)
