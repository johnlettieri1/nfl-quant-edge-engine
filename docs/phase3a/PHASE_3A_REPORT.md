# NFL Totals Engine: Phase 3A Report (Form, Pace and Possessions)

**Branch:** `phase-3a-form-pace`, pushed to GitHub. 5 commits on top of main e0df83d:
1. c5336c0 Freeze feature specification and config before evaluation
2. 52b8ac8 Implement frozen features, candidate harness, evaluation, leakage tests
3. b64e630 Evaluation results on 2015-2022 (no candidate passes the keep rule)
4. d8eee6d Gate report
5. 7812eff Data quality: relocated-team alias normalization (STL/SD/OAK), kept separate from the Phase 3A research

A final documentation-only commit updates this header. Nothing else changed.

**Pull request:** [PR #2](https://github.com/johnlettieri1/nfl-quant-edge-engine/pull/2) into `main`, approved October 15, 2026.
**Date:** October 15, 2026
**Status:**
- Experiment complete; no Phase 3A candidate advances.
- `ridge8_v1` stays live, unchanged.
- Holdout 2023-2025 never loaded.

---

## Bottom line

**The issue:** None of the Phase 3A features created real predictive signal. No candidate passes the keep rule. Realized win rate still does not rise with model edge (best slope p = 0.29), and every candidate that adds new features has *worse* probability quality than ridge8_v1 (Brier 0.2551 to 0.2571 vs 0.2550). The form-only candidate is worse by a margin outside its confidence interval.

**Why:** Every new feature is already in the closing line. The 22 features correlate with the market total at up to 0.61, but with the part the market got wrong (actual minus line) at -0.05 to +0.04, and none survives a multiple-test correction. The model learns what the book already knows, then adds noise. Adding features also pushed the average projection about 0.8 points above the line, so the model picked Over 61-63% of the time, and Overs went 48%.

**Recommendation:** Do not advance any Phase 3A candidate. Keep `ridge8_v1` as the live model version, with no change to the weekly workflow. Restructure Phase 3B around the market instead of around more team stats (section 14). If that fails the same gate, it is the evidence that public-data projection cannot beat the closing total, and the project should pivot to line timing and CLV.

**One real defect found (baseline, not fixed in place):** ridge8_v1 never accumulated EPA history for the Rams through 2015, the Chargers through 2016, or the Raiders through 2019. Play-by-play uses today's team codes (LA, LAC, LV), schedules use the codes of the day (STL, SD, OAK), and the join failed silently. This affects 170 of the 2,079 development games. Fixing it changes nothing measurable (control `c0b` below), but it is fixed in all Phase 3A code and should go into the next model version.

---

## 1. Exact new features and definitions

Full specification: `docs/phase3a/PHASE_3A_FEATURE_SPEC.md`. It was committed (c5336c0) before any candidate was evaluated. Values are in `src/nfl/features/phase3a_config.py`.

**Shared estimator (all new team metrics).** Bayesian-prior exponentially weighted average:
`estimate = (4 x league prior + sum of w x value) / (4 + sum of w)`, with `w = 0.5^(games ago / 6) x 0.5^(offseasons ago)`.
- Half-life is 6 games (h = 12 was run only as a robustness check).
- Last season's games lose half their weight at each offseason.
- The prior is 4 games of the league mean from the previous season.
- At Week 1, a team is about half last season and half league average. After 4 games, the current season carries about a third of the weight.

**Neutral situation (documented definition):** scrimmage play (pass or run, not kneel or spike), downs 1-3, quarters 1-4, more than 2:00 left in the half, offense win probability between 20% and 80%.

| Group | Game-level features (home and away matchup sums) | Per-team inputs |
|---|---|---|
| A. Weighted form (8) | points (off + opp def) x2, EPA/play x2, pass EPA/dropback sum, rush EPA/designed run sum, success rate x2 | off/def points, EPA, success rate, pass EPA, rush EPA |
| B. Pace and PROE (9) | neutral seconds/play, offensive plays, defensive plays faced, no-huddle rate, drives, plays/drive, PROE, neutral dropback rate, defensive PROE faced | Seconds/play is the game-clock gap between consecutive snaps in the same drive (neutral only, gaps over 50 s dropped). PROE is dropback minus nflverse `xpass` in neutral situations. |
| C. Explosives (4) | explosive pass x2, explosive run x2 (offense + defense allowed) | Pass 20+ yards per dropback; designed run 10+ yards |
| D. Expected possessions (1) | `exp_game_possessions` | Mean drive length (offense) and drive length faced (defense) |

**Expected possessions formula:**
```
T_home = home drive seconds x away drive seconds faced / league drive seconds   (multiplicative matchup)
T_away = away drive seconds x home drive seconds faced / league drive seconds
expected possessions per team = 3600 / (T_home + T_away)      (same for both teams; receiver unknown pregame)
expected game possessions     = 2 x that
```
Check: it averages 21.3 per game against 21.7 actual drives in 2015-2022. It correlates 0.27 with actual drives, slightly better than raw weighted drives/game (0.26).

**Reliability windows:**
- No-huddle and PROE are treated as missing before 2006, because nflverse does not populate them reliably before then.
- Seconds/play is treated as missing before 2001, because 1999-2000 game clocks are stamped per drive.
- A missing value leaves the team at its prior.

**Amendments made before evaluation, both from data checks:**
- **A1:** franchise-code normalization, plus a control model for it.
- **A2:** seconds/play reliable from 2001.

## 2. Files added or changed (70 files, about 6,070 lines added, nothing deleted)

| File | Change |
|---|---|
| `src/nfl/data/ingest_style.py` | New. Separate play-by-play cache with the 36 columns Phase 3A needs; defaults to 1999-2022. The baseline cache is untouched. |
| `src/nfl/features/phase3a_config.py` | New. Frozen constants. |
| `src/nfl/features/phase3a_features.py` | New. Within-game aggregation, chronological EW estimator, game-level features, franchise normalization. |
| `src/nfl/backtest/phase3a_harness.py` | New. Builds all candidates beside ridge8_v1 with the identical walk-forward. Writes the Phase 2A prediction schema plus feature columns. |
| `src/nfl/backtest/phase3a_eval.py` | New. Log loss, ECE, slope CIs, per-season, per-side and leave-one-season-out slopes, paired bootstrap vs ridge8_v1, keep rule. |
| `run.py` | Added `data-style` and `phase3a` commands. Existing commands unchanged. |
| `tests/leakage/test_phase3a_leakage.py`, `tests/unit/test_phase3a_ew.py` | New, 11 tests. |
| `docs/phase3a/` | Spec, this report, and all result tables (`results/`). |

**Not touched:**
- `total_model.py`, `totals_harness.py` and the baseline cache
- The weekly workflow (`predict_totals`, ledger, `weekly.yml`) and the `records` branch

## 3. Leakage-test results

**Full suite: 91 passed, 1 skipped** (the optional-XGBoost test), up from 80. Every new feature is covered by these tests:

| Test | What it proves | Result |
|---|---|---|
| Future corruption (h = 6 and h = 12) | Scrambling scores and play-by-play for every game after a cutoff leaves every feature up to the cutoff bit-for-bit identical, and later games do change, so the test is not vacuous | Pass |
| Own-game invariance | A game's features never use that game's own data | Pass |
| Season priors | Season S prior moves only when season S-1 changes | Pass |
| Reliability windows | No-huddle, PROE and seconds/play are missing before their start seasons | Pass |
| Franchise codes | Play-by-play "LV" rows attach to schedule "OAK" rows | Pass |
| Harness equivalence | Generic walk-forward without standardization reproduces ridge8_v1 exactly | Pass |
| Harness holdout invariance | Development predictions are identical with or without holdout and future seasons in the cache (even corrupted); no holdout prediction is written | Pass |
| EW math | Hand-computed estimator, offseason carryover and possession values | Pass |

**Independent review:** a separate reviewer re-checked the code, repeated the corruption test on the real 2015-2022 data at three cutoffs (max difference 0.0), confirmed no 2023+ rows anywhere in the inputs or outputs, and recomputed the headline numbers from the prediction files. Everything matched.

## 4. Candidate models tested

All candidates use the same walk-forward as ridge8_v1:
- Expanding window from 1999, refit each season, at least 500 training games.
- Ridge alpha 10, flat per-season sigma, Normal CDF.
- The probability mapping is deliberately unchanged, so any difference comes from features.

One fitting change: features are standardized on the training fold, because the new features have very different scales. Two controls isolate that change and the team-code fix.

| ID | Features | Role |
|---|---|---|
| ridge8_v1 | 8 baseline | Frozen reference (Phase 2A file) |
| c0 ridge8_std | 8 baseline, standardized | Control: fitting change |
| c0b ridge8_std_teamfix | 8 baseline, team codes fixed | Control: team fix. All candidates build on this |
| c1 form | + Group A | Ablation |
| c2 pace | + Group B | Ablation |
| c3 xpl | + Group C | Ablation |
| c4 poss | + Group D | Ablation (structural piece, separately) |
| **c5 all** | + A + B + C + D (30 features) | **Pre-registered primary** |
| c5 minus form / pace / xpl / poss | Leave one group out | Incremental value with the others present |
| c1 form h12, c5 all h12 | Half-life 12 | Robustness only |

## 5. Ablation results (2015-2022, 2,079 games each)

| Model | Model MAE | Brier | Log loss | ECE | Slope (per pt) | Slope p | Win % | ROI | Over share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ridge8_v1 | 10.762 | 0.2550 | 0.7038 | 0.058 | +0.001 | 0.97 | 50.9% | -2.8% | 51% |
| c0 std | 10.762 | 0.2550 | 0.7037 | 0.058 | +0.009 | 0.72 | 50.7% | -3.2% | 50% |
| c0b team fix | 10.771 | 0.2551 | 0.7038 | 0.060 | +0.020 | 0.45 | 50.2% | -4.0% | 50% |
| c1 + form | 10.795 | 0.2568 | 0.7072 | 0.070 | +0.006 | 0.83 | 49.2% | -6.0% | 61% |
| c2 + pace/PROE | 10.824 | 0.2568 | 0.7074 | 0.067 | +0.003 | 0.89 | 50.0% | -4.6% | 62% |
| c3 + explosives | **10.756** | 0.2551 | 0.7038 | 0.063 | **+0.029** | **0.29** | 49.9% | -4.8% | 55% |
| c4 + possessions | 10.783 | 0.2557 | 0.7051 | 0.062 | -0.004 | 0.87 | 50.6% | -3.4% | 54% |
| **c5 all** | 10.811 | 0.2571 | 0.7079 | 0.083 | +0.008 | 0.75 | 49.3% | -5.8% | 63% |
| c5 minus form | 10.814 | 0.2569 | 0.7075 | 0.076 | +0.021 | 0.40 | 49.3% | -5.8% | 65% |
| c5 minus pace | 10.786 | 0.2566 | 0.7068 | 0.070 | +0.010 | 0.71 | 49.2% | -6.1% | 60% |
| c5 minus xpl | 10.802 | 0.2568 | 0.7072 | 0.080 | +0.013 | 0.63 | 49.1% | -6.2% | 60% |
| c5 minus poss | 10.802 | 0.2568 | 0.7073 | 0.081 | +0.015 | 0.56 | 49.2% | -6.1% | 62% |
| c1 form, h = 12 | 10.796 | 0.2568 | 0.7072 | 0.071 | +0.009 | 0.74 | 49.2% | -6.1% | 61% |
| c5 all, h = 12 | 10.813 | 0.2571 | 0.7078 | 0.078 | +0.016 | 0.53 | 49.1% | -6.2% | 61% |

For comparison:
- Market MAE on the same games is **10.546**. Every model is 0.21 to 0.28 points worse than the closing line.
- A coin flip scores 0.2500 on Brier and 0.6931 on log loss. Every model is worse than a coin flip on both.

**Incremental contribution of each group** (vs c0b, the same fitting with the team fix):
- **Form:** Brier +0.0017, MAE +0.025. Hurts.
- **Pace/PROE:** Brier +0.0017, MAE +0.053. Hurts.
- **Explosives:** Brier 0.0000, MAE -0.015, slope +0.009. Neutral. The only group that does not hurt.
- **Possessions:** Brier +0.0006, MAE +0.013. Slightly hurts.

Removing any one group from c5 changes Brier by 0.0002 to 0.0005 and leaves every slope insignificant, so no group carries the full model.

Half-life 12 gives the same answer as half-life 6, so the conclusion does not depend on the recency setting.

## 6. Baseline vs Phase 3A (primary candidate c5 all)

| Measure | ridge8_v1 | c5 all | Paired difference (95% bootstrap CI) |
|---|---:|---:|---|
| Brier (over) | 0.2550 | 0.2571 | +0.0020 (-0.0003 to +0.0044), worse |
| Model MAE | 10.762 | 10.811 | +0.049 (-0.03 to +0.13), worse |
| Logistic slope, win on edge | +0.001 (CI -0.050 to +0.052) | +0.008 (CI -0.043 to +0.059) | Both indistinguishable from zero |
| Spearman, edge vs win | -0.004 | +0.004 | |
| ECE | 0.058 | 0.083 | Worse |
| Same pick as ridge8_v1 | | 77% of games | Edge correlation 0.80 |

**Form-only candidate (c1):** its Brier is worse than ridge8_v1 by +0.0017, with a CI of +0.0001 to +0.0033. That is the one difference in the study that is statistically clear, and it points the wrong way.

## 7. Edge-bucket results (frozen Phase 2A buckets, 2015-2022, all picks)

Each cell shows games, win % and ROI at -110.

| Edge | ridge8_v1 | c0b team fix | c3 + explosives | c5 all |
|---|---|---|---|---|
| 0.00-0.99 | 620, 51.8%, -1.1% | 614, 49.9%, -4.6% | 598, 50.0%, -4.5% | 580, 52.0%, -0.7% |
| 1.00-1.49 | 261, 51.0%, -2.7% | 261, 52.3%, -0.1% | 298, 49.7%, -5.1% | 239, 39.6%, -24.0% |
| 1.50-1.99 | 238, 48.7%, -7.0% | 240, 50.2%, -4.1% | 255, 46.9%, -10.5% | 265, 48.1%, -8.1% |
| 2.00-2.49 | 217, 50.2%, -4.0% | 214, 45.9%, -12.0% | 223, 40.5%, -22.5% | 227, 52.9%, +1.0% |
| 2.50-2.99 | 194, 49.2%, -6.0% | 192, 49.0%, -6.5% | 172, 52.9%, +1.1% | 192, 43.4%, -16.9% |
| 3.00-3.99 | 260, 50.0%, -4.5% | 268, 49.2%, -5.9% | 250, 54.3%, +3.5% | 261, 51.9%, -0.8% |
| 4.00+ | 289, 53.0%, +1.1% | 290, 54.0%, +3.1% | 283, 54.1%, +3.2% | 315, 51.6%, -1.5% |
| **Edge 2.0+** | 960, 50.8%, -3.0% | 964, 49.9%, -4.7% | 928, 50.7%, -3.3% | 995, 50.4%, -3.7% |
| **Edge 3.0+** | 549, 51.6%, -1.6% | 558, 51.7%, -1.3% | 533, 54.2%, +3.4% (CI 49.8-58.5) | 576, 51.7%, -1.2% |

**Does realized win rate rise with edge? No, for any candidate.** The buckets bounce up and down with no structure, just as in Phase 2A.

**c3 3.0+ (54.2%):** this is the best cumulative cell. But its CI reaches below breakeven, its 2.0-2.49 bucket is 40.5%, and it is one of roughly 270 cells across 13 models. It is not a signal.

**Over vs Under:**

| | ridge8_v1 | c1 form | c2 pace | c3 xpl | c5 all |
|---|---|---|---|---|---|
| Over picks: games, win % | 1,040, 49.0% | 1,258, 47.9% | 1,287, 48.5% | 1,125, 48.2% | 1,292, 48.0% |
| Under picks: games, win % | 1,018, 52.8% | 800, 51.4% | 771, 52.4% | 933, 51.9% | 766, 51.6% |
| Slope, Over picks | -0.027 | -0.008 | -0.016 | -0.005 | -0.005 |
| Slope, Under picks | +0.025 | +0.061 | +0.076 | +0.070 (p 0.08) | +0.063 |

Two cautions on the Under side.

**What the numbers show:**
- Large-edge Unders look good in several candidates: 3.0-3.99 Unders won 58-65% in c1, c2, c3 and c5.
- Under slopes are positive and Over slopes are negative in ridge8_v1 and all six main candidates.

**Why it is not evidence:**
- 51.9% of 2015-2022 games went Under the closing total, against 48.9% in 2010-2014. Part of this is just the era.
- The Over-side slope is negative everywhere. The "both sides" rule exists for exactly this pattern.
- This is the same Under pattern Phase 2A flagged, and it was not significant there either.

## 8. Calibration comparison (stated vs realized win %)

| Model says | ridge8_v1: games, stated, realized | c0b | c3 xpl | c5 all |
|---|---|---|---|---|
| 50-52% | 421, 51.0, 52.0 | 431, 51.0, 49.2 | 393, 51.0, 49.9 | 410, 51.0, 53.9 |
| 52-54% | 389, 53.0, 51.2 | 371, 53.0, 51.5 | 414, 53.0, 48.8 | 335, 53.0, 43.0 |
| 54-56% | 319, 55.0, 49.8 | 328, 55.0, 49.7 | 356, 55.0, 48.3 | 354, 55.0, 47.7 |
| 56-58% | 289, 56.9, 48.1 | 277, 56.9, 48.7 | 292, 56.9, 44.9 | 285, 57.0, 49.8 |
| 58-60% | 218, 58.9, 49.5 | 236, 58.9, 48.7 | 189, 59.0, 54.5 | 221, 58.9, 49.3 |
| 60%+ | 422, 63.7, 52.8 | 415, 63.7, 52.5 | 414, 63.6, 53.6 | 453, 63.7, 50.8 |
| **ECE** | **0.058** | 0.060 | 0.063 | 0.083 |
| **Gap in 56%+ bins** | **9.9 pts** | 10.0 | 9.5 | 10.5 |

The overconfidence Phase 2A found is unchanged: when a model says 60%+, it wins 51-54%. c3 narrows the high-confidence gap by 0.4 points, which is noise. Calibration was not the target of Phase 3A, but it confirms that the features did not make the model's disagreements with the market more trustworthy.

## 9. Season stability (win % on all picks)

| Season | ridge8_v1 | c0b | c1 form | c2 pace | c3 xpl | c4 poss | c5 all |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2015 | 50.6 | 50.6 | 50.6 | 47.4 | 47.4 | 48.6 | 47.8 |
| 2016 | 42.4 | 42.7 | 44.7 | 45.9 | 48.6 | 44.3 | 47.8 |
| 2017 | 53.1 | 52.0 | 47.7 | 54.7 | 50.8 | 52.7 | 48.4 |
| 2018 | 48.2 | 48.2 | 48.6 | 46.2 | 45.5 | 49.4 | 48.2 |
| 2019 | 54.1 | 53.3 | 48.6 | 50.6 | 52.9 | 52.2 | 51.8 |
| 2020 | 53.4 | 51.8 | 54.2 | 51.8 | 49.8 | 53.8 | 49.0 |
| 2021 | 52.8 | 52.0 | 47.2 | 50.2 | 50.9 | 50.9 | 48.0 |
| 2022 | 52.2 | 51.1 | 52.2 | 52.6 | 52.6 | 52.6 | 53.4 |
| Seasons with positive edge slope | 4 of 8 | 7 of 8 | 4 of 8 | 5 of 8 | 6 of 8 | 5 of 8 | 6 of 8 |

**Season slopes:** positive season slopes are small and none is significant. 2016 and 2022 are negative for most models.

**Bias by season:** the new-feature models projected above the line in 6 or 7 of 8 seasons, by about +0.8 points on average for c1, c2 and c5. ridge8_v1 averaged -0.02. This is a level-drift problem. The model is trained on 1999-2014, when the link between pace, efficiency and points sat at different levels, and it projects too high once the market has adjusted.

**Replication 2010-2014 (supplementary, never used to choose):**
- Most models show a positive, sometimes significant, slope in that era:
  - ridge8_v1 p = 0.08
  - c0 p = 0.007
  - c0b p = 0.03
  - c3 p = 0.001
  - c5 p = 0.12
- Because the plain standardization control c0 is also significant, this is an era effect that faded by 2015, not something the new features add.
- Phase 2A saw the same 2010-2014 shine disappear in 2015-2022.

## 10. Which features helped

None met the bar. The least-bad results:
- **Explosive-play environment (Group C):** the only group that did not worsen Brier or MAE. It also had the highest edge slope (+0.029, p = 0.29) and was positive in 6 of 8 seasons and with every season left out. It still fails the slope, Brier, ECE and both-sides tests. The right reading is "harmless, possibly a weak signal worth re-testing in a market-anchored setup," not "helps."
- **Franchise-code fix:** correct and necessary. It is neutral on results.
- **Expected possessions as a measurement:** it predicts actual possessions (r = 0.27) better than raw drives/game. As a totals feature, it is already priced.

## 11. Which features failed

**Model results:**
- **Weighted form (Group A):** worst result. It made Brier measurably worse (the CI excludes zero), raised MAE, and pushed the model to 61% Overs. Weighted points and EPA are the most market-known numbers in football (r with the closing total about 0.5 to 0.6).
- **Pace and PROE (Group B):** highest MAE of any group (+0.05), with the same Over drift. No-huddle rate has essentially zero relationship with the closing total (r 0.01) or with the result.
- **Expected possessions (Group D):** negative edge slope; slight Brier loss.
- **All combined (c5):** worse than its parts on calibration (ECE 0.083). More features led to more confident disagreement with the market, and that disagreement was mostly wrong.

**Post-hoc check** (`results/posthoc_feature_vs_market.csv`, done after the frozen evaluation and not used to pick anything):
- Correlation with actual minus closing line: between -0.05 and +0.04 for all 22 features. 0 of 22 are significant after correcting for 22 tests.
- Correlation with the closing line itself: up to 0.61.

**That is the core finding: the book already prices team form, pace, tendency, explosiveness and possession count.**

## 12. Did Phase 3A produce genuine signal?

**No.** Here is each candidate scored against the pre-registered keep rule:

| Rule (all must pass) | c0b | c1 | c2 | c3 | c4 | c5 |
|---|---|---|---|---|---|---|
| 1. Edge slope > 0, p < 0.05 | No | No | No | No | No | No |
| 2. Brier below 0.2550 | No | No | No | No | No | No |
| 3a. ECE below 0.058 | No | No | No | No | No | No |
| 3b. Smaller 56%+ calibration gap | No | No | No | Yes | No | No |
| 4a. Slope positive in 5+ of 8 seasons | Yes | No | Yes | Yes | Yes | Yes |
| 4b. Slope positive leaving any season out | Yes | No | No | Yes | No | No |
| 5. Slope positive for Overs and Unders | No | No | No | No | No | No |
| **Advance** | **No** | **No** | **No** | **No** | **No** | **No** |

The leave-one-out and half-life variants also fail rules 1 to 3. Nothing was tuned after the freeze, and no betting threshold was examined or optimized.

## 13. Recommended model version to carry forward

**`ridge8_v1`, unchanged, as the live weekly model.** It keeps posting leans at 0 units, as it does now. Nothing in the weekly workflow changes.

- **No `phase3a_v1` is created.** No candidate earned it.
- **The team-code fix is ready** as a separate data-quality module (`src/nfl/data/team_aliases.py`, details and impact in `DATA_QUALITY_RELOCATED_TEAMS.md`). It should become part of the next model version that changes for a real reason, not shipped alone, because on its own it changes nothing measurable.
- **The Phase 3A code stays in the repo** as research tooling. It is not in the weekly path.

## 14. Recommendation for Phase 3B

The evidence from Phase 2A and 3A now points one way. A model that projects the total from team statistics and then compares it to the line has nothing to find, because the line already contains those statistics. More inputs of the same kind (weather, injuries) bolted onto the same structure will likely fail the same way. My recommendation is to change the structure before adding inputs.

**Proposed Phase 3B (same gates, same frozen periods):**
1. **Market-anchored target.** Predict how far the result lands from the line (actual minus closing total), using the line as the anchor. A feature then has to explain what the book missed. This also removes the level drift that caused the 0.8-point Over bias. This was planned for 3C; I would move it first.
2. **Calibration layer before any staking math.** Shrink stated probabilities toward 50%, fit walk-forward on development seasons only. This is required no matter what else happens. Today's 60%+ calls win 51-54%.
3. **Then weather and injuries, inside the anchored model.** Their value is timing: news that arrives after the opener and before the close.
4. **Re-test the explosives group** inside the anchored model. It was the only Phase 3A group that did not hurt.
5. **Pre-register the "large-edge Under" pattern as a single hypothesis for the 2026 forward test only.** It has now appeared in Phase 2A and 3A. It is not to be acted on, and it is not to be tested on the holdout.

**Known limitation:**
- nflverse provides only the closing total, so historical backtests can only measure edge against the close.
- Edge against the opener needs a paid historical-odds source or the forward Tuesday snapshots, which start October 20.

**Stop point:** if a market-anchored model also fails this gate, I would treat it as the answer, not a reason to keep adding features. At that point, the engine's best use is disciplined line shopping and CLV tracking against the opener, and I would recommend that pivot.

---

## Approval needed

1. Accept the Phase 3A result: no candidate advances, and ridge8_v1 stays live.
2. Push `phase-3a-form-pace` to your fork as a record. My recommendation is to open a PR so the research code, tests and results are preserved in main. It does not touch the weekly path. Or keep it unmerged; your call.
3. Approve the restructured Phase 3B (market-anchored target and calibration first, then weather and injuries), or tell me to run 3B as originally planned.

*Not betting or financial advice. These are backtest statistics, not guarantees.*
