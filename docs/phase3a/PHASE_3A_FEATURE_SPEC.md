# Phase 3A Feature Specification (FROZEN before evaluation)

Status: **frozen**. Written and committed before any Phase 3A candidate was evaluated
against game outcomes. Every choice below was made from football logic and from
data-coverage checks only (which seasons have a column populated). No feature
definition, half-life, threshold, or window was chosen by looking at totals results.

Frozen config values live in `src/nfl/features/phase3a_config.py`. The commit that
adds this file and that config is the freeze point; the evaluation commit comes after it.
Any change after the freeze is an amendment, recorded at the bottom of this file with
the reason, and the affected candidates are re-run and reported both ways.

---

## 0. Periods (unchanged, permanent)

| Period | Seasons | Use in Phase 3A |
|---|---|---|
| Prior history | 1999-2014 | Feature construction and model training only |
| Development | 2015-2022 regular season, games with a closing total | All decisions |
| Replication (supplementary) | 2010-2014 | Reported only; never used to choose anything |
| Holdout | 2023-2025 | **Not loaded.** Style play-by-play is pulled through 2022 only; the harness drops schedule rows >= 2023 before any computation and asserts it |
| Forward | 2026 Weeks 7-18 | Prospective only, not part of Phase 3A |

## 1. Shared estimator: Bayesian-prior exponentially weighted (EW) team form

Every Phase 3A team metric uses the same estimator, so there is one recency knob, not one per feature.

For team T, metric m, attached to game g (strictly before kickoff):

```
estimate = (k * prior_m(S) + SUM_i w_i * x_i) / (k + SUM_i w_i)
w_i      = 0.5 ** (a_i / h)  *  c ** b_i
```

- `x_i` = the metric in T's earlier game i (only games completed before g).
- `a_i` = number of T's games played after game i and before g (most recent game: a = 0).
- `b_i` = number of season boundaries between game i and g (last season's games: b = 1).
- `h` = half-life in games. **Primary h = 6. Sensitivity h = 12.** No other values are run.
- `c` = offseason carryover factor = **0.5** (each offseason halves the weight of older seasons).
- `k` = prior strength in games = **4**.
- `prior_m(S)` = league mean of metric m over all team-games of season S-1. If S-1 has no
  observed values for m (1999 for everything; 2006 for PROE and no-huddle), a fixed neutral
  default is used (table in section 6).

Why these values (football logic, not fitted):
- **h = 6**: the Master System weights last 4 games 35%, last 8 games 30%, season 25%, prior 10%.
  That recency profile is close to an exponential with a 5-7 game half-life. 6 sits in the middle.
  h = 12 is the "stable team quality" alternative and is reported only as a robustness check.
  **If h = 6 and h = 12 disagree, h = 6 remains the result of record.**
- **c = 0.5**: year-over-year correlation of team offensive EPA/play and points is roughly 0.4-0.5,
  defense lower. Halving last season's weight at each offseason reflects that roster/coach turnover.
- **k = 4**: at Week 1 a team's estimate is about half last season, half league mean (consistent with
  the year-to-year correlation above). After 4 games the current season carries about a third of the
  weight, the prior season a quarter, and the league mean the rest. This is the "regress toward league
  average when sample sizes are small" requirement.
- `x_i` missing for a game (column not populated) contributes nothing; the estimate stays at prior.
- Every per-game value is aggregated **within one game only**; the chronological pass attaches the
  estimate before the game, then adds that game. Same pattern as the existing leak-free features.

## 2. Play filters (exact)

- **Scrimmage play P**: `play == 1` and (`pass == 1` or `rush == 1`) and `epa` not null,
  excluding `qb_kneel == 1` and `qb_spike == 1`.
- **Dropback**: P and `qb_dropback == 1` (pass attempts, sacks, scrambles).
- **Designed run**: P and `rush == 1` and `qb_dropback == 0`.
- **Neutral situation N** (documented definition): P and `down` in {1, 2, 3} and `qtr` <= 4 and
  `half_seconds_remaining` > 120 and 0.20 <= `wp` <= 0.80 (offense's pre-snap win probability).
  This is the common public-analytics definition (competitive game state, no two-minute drill,
  no overtime, no fourth-down decisions).

## 3. Per-team-game metrics (within-game aggregation)

### Group A: weighted form (replaces nothing; supplements the flat 16-game and 10-game windows)

| Metric | Definition |
|---|---|
| `off_pts` | Points scored (schedules, includes OT) |
| `def_pts` | Points allowed |
| `off_epa` | Mean EPA per scrimmage play P, offense |
| `def_epa` | Mean EPA per P allowed, defense |
| `off_pass_epa` | Mean EPA per dropback |
| `off_rush_epa` | Mean EPA per designed run |
| `off_sr` | Success rate over P, offense |
| `def_sr` | Success rate over P allowed, defense |

### Group B: neutral pace and pass tendency

| Metric | Definition | Reliable from |
|---|---|---|
| `spp` | (reliable from 2001, see A2) Neutral seconds per play: for consecutive P plays by the same offense in the same `fixed_drive`, the game-clock gap `gsr(prev) - gsr(this)` where the later play is in N. Gaps outside (0, 50] seconds are dropped (stoppages, reviews, quarter edges). Needs >= 5 gaps, else missing. Lower = faster. | **2001** |
| `off_plays` | Count of P plays plus kneels and spikes (`play == 1`, pass or rush), offense | 1999 |
| `def_plays` | Same count, faced by the defense | 1999 |
| `nohuddle` | Mean `no_huddle` over P | **2006** (0.0% 1999-2001, under 2% 2002-2005: not reliable; treated as missing before 2006) |
| `drives` | Distinct `fixed_drive` with at least one P play, offense | 1999 |
| `plays_per_drive` | `off_plays / drives` | 1999 |
| `neutral_pass` | Mean of `qb_dropback` over N | 1999 |
| `proe` | Mean of (`qb_dropback` - `xpass`) over N with `xpass` present | **2006** (xpass not populated before 2006) |
| `def_proe_faced` | Same as `proe`, for the opponents' plays against this defense | **2006** |

### Group C: explosive-play environment

| Metric | Definition |
|---|---|
| `xpl_pass` | Share of dropbacks with `yards_gained` >= **20** (offense) |
| `xpl_rush` | Share of designed runs with `yards_gained` >= **10** (offense) |
| `xpl_pass_allowed` | Same, allowed by the defense |
| `xpl_rush_allowed` | Same, allowed by the defense |

Thresholds 20+ pass / 10+ run are the standard public "explosive play" convention. Not tuned.

### Group D: expected possessions (inputs)

| Metric | Definition |
|---|---|
| `drive_secs` | Mean `drive_time_of_possession` (seconds) over the offense's drives with >= 1 P play |
| `drive_secs_faced` | Same, for the opponents' drives against this defense |

## 4. Game-level model features

Totals depend on both teams, so features are matchup sums (offense + the defense it faces).
EW estimates are taken before kickoff for home team H and away team A.

**Group A, form (8)**
`form_pts_home = off_pts[H] + def_pts[A]`, `form_pts_away = off_pts[A] + def_pts[H]`,
`form_epa_home = off_epa[H] + def_epa[A]`, `form_epa_away = off_epa[A] + def_epa[H]`,
`form_pass_epa_sum = off_pass_epa[H] + off_pass_epa[A]`, `form_rush_epa_sum = off_rush_epa[H] + off_rush_epa[A]`,
`form_sr_home = off_sr[H] + def_sr[A]`, `form_sr_away = off_sr[A] + def_sr[H]`.

(Note for the record: the baseline's `home_epa_matchup` is `off_epa - opponent def_epa_allowed`, a
subtraction inherited from the margin model. For totals a worse defense should *raise* scoring, so
Phase 3A uses a sum. The baseline is not changed.)

**Group B, pace/PROE (9)**
`pace_spp_sum = spp[H] + spp[A]`, `pace_off_plays_sum = off_plays[H] + off_plays[A]`,
`pace_def_plays_sum = def_plays[H] + def_plays[A]`, `pace_nohuddle_sum`, `pace_drives_sum`,
`pace_ppd_sum` (plays per drive), `proe_sum = proe[H] + proe[A]`, `neutral_pass_sum`,
`def_proe_faced_sum = def_proe_faced[H] + def_proe_faced[A]`.

**Group C, explosives (4)**
`xpl_pass_home = xpl_pass[H] + xpl_pass_allowed[A]`, `xpl_pass_away = xpl_pass[A] + xpl_pass_allowed[H]`,
`xpl_rush_home = xpl_rush[H] + xpl_rush_allowed[A]`, `xpl_rush_away = xpl_rush[A] + xpl_rush_allowed[H]`.

**Group D, expected possessions (1 model feature, 3 reported values)**

```
T_H = drive_secs[H] * drive_secs_faced[A] / prior_drive_secs(S)     # home offense drive length vs this defense
T_A = drive_secs[A] * drive_secs_faced[H] / prior_drive_secs(S)
exp_poss_home = exp_poss_away = 3600 / (T_H + T_A)                  # regulation clock shared by alternating drives
exp_game_possessions = exp_poss_home + exp_poss_away                # the model feature
```

Multiplicative matchup (offense tendency x defense tendency / league mean) is the standard log5-style
adjustment. Possessions alternate, so both teams get the same expected count (the receiving team is
not known pregame; difference is at most one). Only the possession count enters the model; a
possessions x points-per-possession structural projection is deferred to a later phase as instructed.

## 5. Candidate models

All candidates use the identical walk-forward as ridge8_v1: for season S, fit on all completed games
with season < S (expanding window from 1999, minimum 500 training games), Ridge alpha = 10, flat
per-season sigma (training residual SD), Normal CDF for P(over). The probability mapping is not
changed in Phase 3A, so differences come from features only.

One fitting change is required: Phase 3A features are on very different scales (rates near 0.1,
seconds near 28), so candidates standardize features (StandardScaler fit on the training fold only)
before Ridge. To separate this change from the features, a control candidate runs the 8 baseline
features with the same standardization.

| ID | Features | Role |
|---|---|---|
| `ridge8_v1` | 8 baseline | Frozen reference (Phase 2A numbers, not re-fit) |
| `p3a_c0_ridge8_std` | 8 baseline, standardized | Control for the fitting change |
| `p3a_c0b_ridge8_std_teamfix` | 8 baseline on franchise-normalized codes, standardized | Control for the team-code fix (amendment A1); reference for incremental value |
| `p3a_c1_form` | 8 + Group A (8) | Ablation |
| `p3a_c2_pace` | 8 + Group B (9) | Ablation |
| `p3a_c3_xpl` | 8 + Group C (4) | Ablation |
| `p3a_c4_poss` | 8 + Group D (1) | Ablation, structural component separately |
| `p3a_c5_all` | 8 + A + B + C + D (30) | **Pre-registered primary candidate** |
| `p3a_c5_all_minus_{form,pace,xpl,poss}` | all minus one group | Leave-one-group-out (incremental value in presence of the others) |
| `p3a_c1_form_h12`, `p3a_c5_all_h12` | same, h = 12 | Half-life robustness only |

Incremental contribution of a group = that candidate vs `p3a_c0_ridge8_std` (same fitting), and
the all-minus-group candidate vs `p3a_c5_all`.

Every candidate writes the Phase 2A prediction schema (`PRED_COLS`, same order) with its own
feature columns appended, so `totals_harness.analyze` evaluates it unchanged.

## 6. Fixed neutral defaults (used only when the prior season has no data)

points 22.0; EPA 0.0; success rate 0.45; pass EPA 0.0; rush EPA 0.0; seconds/play 28.0;
plays 63.0; no-huddle 0.04; drives 11.5; plays/drive 5.5; neutral dropback rate 0.55; PROE 0.0;
explosive pass 0.08; explosive rush 0.10; drive seconds 160.0.

## 7. Phase 3A keep rule (fixed now)

A candidate advances to Phase 3B only if **all** hold on 2015-2022:

1. **Edge relationship**: logistic slope of win on |edge| is positive with two-sided p < 0.05
   (the Phase 2A criterion). A positive slope with p >= 0.05 is reported as "not established".
2. **Probability quality**: Brier for P(over) below ridge8_v1's 0.2550 (paired bootstrap CI reported).
3. **Calibration**: ECE below ridge8_v1's, and the average |realized - stated| gap in the bins where
   the model states >= 56% is smaller than ridge8_v1's.
4. **Season stability**: slope positive in at least 5 of 8 seasons, and still positive when any one
   season is dropped (leave-one-season-out).
5. **Both sides**: slope point estimate positive for Over picks and for Under picks separately.

Raw win % and ROI are reported but are not a keep criterion. Betting thresholds are not optimized.

## 8. Metrics reported for every candidate

Games evaluated, total MAE, market MAE, Brier (over), log loss, calibration table, ECE, Spearman
|edge| vs win, logistic slope with Wald p-value and 95% CI (plus bootstrap CI), win rate and ROI at
-110 by the frozen Phase 2A edge buckets, Over vs Under, season-by-season, bootstrap CIs, and paired
bootstrap differences vs ridge8_v1 (Brier, log loss, MAE).

ECE definition: games binned by stated P(pick wins) into the Phase 2A bins
[50,52), [52,54), [54,56), [56,58), [58,60), [60,100]; ECE = sum over bins of (n_bin / n) x
|realized win % - mean stated %|. Pushes excluded.

Log loss: on P(over) vs the Over outcome, pushes excluded, probabilities clipped to [1e-6, 1 - 1e-6].

---

## Amendments after freeze

All amendments below were made during implementation, **before any candidate was evaluated
against outcomes**, and are driven by data defects found in coverage checks, not by results.

**A1. Franchise code normalization, and a second control (`p3a_c0b_ridge8_std_teamfix`).**
nflverse play-by-play labels every season with the current franchise code (LA, LAC, LV), while
schedules use the code of the day (STL through 2015, SD through 2016, OAK through 2019). The
existing trailing-EPA feature joins the two by code, so in ridge8_v1 the Rams 1999-2015, Chargers
1999-2016 and Raiders 1999-2019 never accumulate EPA history (their side of the EPA matchup stays
at the 0.0 default), and the flat 16-game scoring window restarts at each relocation. Phase 3A
normalizes both sides to the current franchise code. To keep the comparison honest, a second
control runs the 8 baseline features recomputed on normalized team codes (standardized), and every
Phase 3A candidate is built on that corrected baseline. Incremental value of each group is measured
against `p3a_c0b_ridge8_std_teamfix`. ridge8_v1 itself is not changed.

**A2. Neutral seconds per play reliable from 2001.** In 1999 nflverse game-clock stamps repeat
for every play in a drive (69% zero gaps); in 2000, 13% of games are partly affected (10% zero gaps).
From 2001 on, zero gaps are 2% or less. `spp` is treated as missing before 2001 (estimate stays at
the prior), the same rule already used for no-huddle and PROE before 2006.
