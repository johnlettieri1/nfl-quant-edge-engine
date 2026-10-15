# NFL Totals Engine: Phase 3B Gate Report (Market-Residual Model and Probability Calibration)

**Branch:** `phase-3b-market-residual` (from main 5130cbe). Not merged; no PR opened yet.
**Date:** October 15, 2026
**Status:**
- Experiment complete. **No Phase 3B model passes the pre-registered keep rule.**
- `ridge8_v1` stays live, unchanged. The weekly pipeline, records branch, odds snapshots and CLV ledger were not touched.
- Holdout 2023-2025 never loaded. All research pulls stop at 2022.

---

## Bottom line

**The issue:** Predicting where the closing total is wrong does not work with public football data. Every residual model built from the ridge8_v1 or Phase 3A features has a zero or slightly *negative* correlation with the market's actual error (Pearson -0.050 to -0.003) and a negative out-of-sample R², meaning each one predicts the market's error worse than assuming the market is right. Injuries and QB changes add nothing.

**One lead, not a pass:** observed game-day weather (wind, rain) is the only input that predicts the market's error: Pearson +0.066 (p = 0.003), positive in 7 of 8 seasons and with any season left out. It still fails the rule that matters most: bigger predicted edges do not win more often (slope p = 0.23). It also uses weather observed at kickoff, not a forecast, so it overstates what a bettor could have known.

**Calibration works, and it shows the truth:** the calibration layer fixes ridge8_v1's overconfidence (ECE 0.058 to 0.012, Brier 0.2550 to 0.2504). But the honest probabilities it produces sit at 50-55%, and no model's calibrated probability beats the no-vig market probability (Brier 0.2500).

**Recommendation:** Do not carry any model forward. Keep ridge8_v1 as the frozen benchmark at 0 units. Pivot the project to market timing, line shopping, opener-to-close movement and CLV, using the 2026 snapshots you are already collecting, and carry weather forward only as a forecast-based hypothesis on 2026 games (section 18).

---

## 1. Residual target definition

```
residual         = actual final total - closing total        (nflverse total_line, same close as every earlier phase)
predicted total  = closing total + predicted residual
edge (points)    = predicted residual;   pick = sign of the predicted residual
```
- Pushes stay in the regression target. They are excluded from win %, Brier and log loss.
- **This is a close-based research benchmark.** It is not evidence that any advantage existed at an earlier betting time. No historical opener was used or manufactured.
- Residual MAE and final-total MAE are the same number by construction. Market MAE is the zero-residual model.

## 2. Candidate architecture

All models use the same walk-forward:
- For season S, fit on all games before S (from 1999, at least 500 games, all game types), then predict S.
- Ridge regression, with features standardized on the training fold only.
- **Ridge strength is chosen inside the walk-forward.** It uses the 5 seasons before S as inner folds, so the test season never influences it. Heavy shrinkage means predicting "the market is right".

What the inner folds chose is itself a finding: for r1 in 2017-2021, and for r2 in every season, they picked the strongest shrinkage on the grid or one step below it (alpha 10,000 to 100,000). The data's own verdict was to trust the market.

**r3 (conservative)** re-selects feature groups each season using inner folds only. It picked explosives in 2015 and 2017, and form from 2018 to 2022.

Spec, frozen before evaluation (commit 6354526): `docs/phase3b/PHASE_3B_SPEC.md`.

## 3. Exact features tested

| Model | Inputs |
|---|---|
| `m0_market` | Nothing: predicted residual 0, P(over) = no-vig closing probability |
| `r0_line` | Closing total |
| `r1_ridge8` | Closing total + 8 ridge8_v1 features (relocated-team codes fixed); `r1o` = original codes |
| `r1_form` / `r1_pace` / `r1_xpl` / `r1_poss` | r1 + one Phase 3A group (8 / 9 / 4 / 1 features) |
| `r2_phase3a` | r1 + all 22 Phase 3A features |
| `r3_conservative` | r1 + groups chosen inside the walk-forward |
| `*_wx` | + weather (7): wind, wind 15+, extreme flag, cold, precipitation, snow, missing flag |
| `*_inj` | + injuries (6): QB, OL, WR/TE, RB, front seven, secondary. Each is status weight x prior 4-game snap share, home + away |
| `*_qb` | + QB (4): starter change, backup flag, first career start, starter's EPA minus previous starter's EPA |
| `r1_all_context` | r1 + weather + injuries + QB (exploratory only) |

Each context group was added to r1 (primary) and to r0 (sensitivity) separately. The injury models train from 2013 and are compared against r1/r0 controls fitted on the same 2013+ window.

## 4. Calibration methodology

- **Raw (reference only):** Phi(r / sigma), the old flat Normal-CDF mapping.
- **Calibrated:** for season S, the calibrator is fitted only on out-of-sample predictions from seasons before S, with at least 500 rows. **No game calibrates itself.**
- Three calibrators were run on every model:
  - logistic (P = a + b x edge)
  - shrink toward 50%
  - isotonic
- **Choice rule (pre-registered):** the simplest method within 0.001 log loss of the best. Shrink was chosen for most models, and logistic for the weather models.
- **What the calibrator learned about ridge8_v1:** keep only 30-40% of the model's distance from 50%, so the shrink factor falls from 0.40 in 2015 to 0.30 in 2022. In logistic terms, a 4-point edge is worth about 54%, not the 61% the Normal CDF claims.

## 5. Leakage and timestamp protections

**Tests:** full suite **112 passed, 1 skipped** (14 new Phase 3B tests). They prove:
- Corrupting every later season leaves all residual predictions and calibrated probabilities unchanged, including the nested alpha and group choices.
- Corrupting a season's own outcomes leaves that season's predictions and probabilities unchanged.
- Each calibrator trains only on prior seasons.
- An injury report row modified at or after kickoff has no effect.
- A game's own snap counts and QB EPA, and any later starters, never feed its features.
- Indoor games carry no weather signal.
- Research pulls refuse 2023+.

**Independent review** (a separate reviewer, not the builder):
- Repeated the corruption test on real 2015-2022 data at cutoffs 2016 and 2019: max difference 0.0.
- Confirmed no 2023+ row in any input or output.
- Refit the logistic calibrator by hand (matched to 5e-9).
- Hand-checked 3 injury games against raw reports and snap counts.
- Recomputed every headline number (0 mismatches).
- Found one real defect (Amendment A1, below).

**Timing, by group:**

| Group | Research availability | Live availability |
|---|---|---|
| Closing total | At kickoff | Close only; earlier lines come from the 2026 snapshots |
| Weather | **Observed at kickoff, not a forecast.** Overstates knowability | Must use forecasts available at prediction time |
| Injuries | Final report, median 54 hours before kickoff; rows edited after kickoff dropped (25 of 53,054) | Final report posts Friday; Tuesday info is the earlier practice reports |
| QB starter | Actual starter, public by the inactive list, about 90 minutes before kickoff | Announced or projected starter |

**Amendment A1:** nflverse snap counts start in 2013, not 2012, so the 2012 injury features were all zero in the first run. The window was moved to 2013 and the injury models were re-run. Both runs are kept; the conclusion is the same.

## 6. Baseline comparisons (2015-2022 regular season, 2,079 games, all models on the identical game set)

| Model | Residual MAE | OOS R² | Pearson r vs residual (p) | Win % | ROI at -110 | Over picks | Win-vs-edge slope p | Calibrated Brier | Calibrated ECE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **m0_market** | **10.546** | 0 | n/a | n/a | n/a | n/a | n/a | **0.2500** | 0.005 |
| ridge8_v1 | 10.762 | -0.048 | -0.012 (0.57) | 50.9% | -2.8% | 51% | 0.97 | 0.2504 (raw 0.2550) | 0.012 (raw 0.058) |
| p3a_c3_xpl | 10.756 | -0.045 | -0.008 (0.70) | 49.9% | -4.8% | 55% | 0.29 | 0.2503 (raw 0.2551) | 0.021 (raw 0.063) |
| r0_line | 10.578 | -0.003 | -0.013 (0.54) | 48.2% | -7.9% | 93% | 0.68 | 0.2501 | 0.020 |
| r1_ridge8 | 10.600 | -0.007 | -0.039 (0.07) | 48.3% | -7.8% | 90% | 0.63 | 0.2504 | 0.025 |
| r1o (old codes) | 10.607 | -0.007 | -0.050 (0.02) | 47.5% | -9.2% | 91% | 0.99 | 0.2506 | 0.033 |
| r2_phase3a | 10.583 | -0.002 | -0.011 (0.61) | 48.4% | -7.4% | 99% | 0.99 | 0.2502 | 0.020 |
| r3_conservative | 10.598 | -0.007 | -0.037 (0.09) | 48.1% | -8.1% | 88% | 0.81 | 0.2503 | 0.025 |

**The market wins every row.** No model has lower MAE than the closing total. r1_ridge8 is measurably worse than the market: paired squared-error difference +1.21, CI +0.14 to +2.27.

**Why the residual models picked Over 88-99% of the time:**
- From 1999 to 2014, games finished an average of +0.89 points over the close. From 2015 to 2022, the average was +0.14, and 51.8% of decided games went Under.
- The models' intercept learned the old "closes were set too low" bias (+0.7 to +0.9 points). The market had already fixed it.
- The correlation numbers ignore the intercept, and they are zero or negative too. Removing the intercept would not rescue these models.

## 7. Residual prediction results

- **Larger predicted residuals do NOT go with larger realized residuals.** OLS slope of actual on predicted residual:
  - r1: -0.89 (CI -1.86 to +0.09)
  - r2: -0.63 (CI -3.03 to +1.78)
  - A useful model would show a slope near +1.
- **Spearman:** -0.04 to -0.001 for all 3B-1 residual models.
- **Direction:** sign accuracy is 47.5-48.9%. That is the Over lean losing in an Under era.

## 8. Edge vs win relationship (the key question)

**Does a larger predicted market error mean a higher chance of beating the market? No, for every model.**

| Model | Slope per point (bootstrap 95% CI) | p |
|---|---|---:|
| ridge8_v1 | +0.001 (-0.050 to +0.054) | 0.97 |
| r1_ridge8 | -0.047 (-0.233 to +0.144) | 0.63 |
| r2_phase3a | +0.003 (-0.375 to +0.393) | 0.99 |
| r3_conservative | -0.022 (-0.202 to +0.153) | 0.81 |
| r0_wx (best context) | +0.085 (-0.050 to +0.225) | 0.23 |

**Edge buckets (frozen Phase 2A bands, games, win %, ROI at -110):** the residual models rarely produce big edges, which is the shrinkage doing its job, and the edges they do produce lose.
- **r1_ridge8**
  - Edge 1.0+: 271 games, 48.1%, -8.1%
  - Edge 2.0+: 61 games, 46.7%, -10.7%
  - Edge 3.0+: 11 games, 18.2%
- **r2_phase3a**
  - Never produces an edge of 2.0 or more
  - Edge 1.0+: 132 games, 44.6%, -14.6%
- **r0_wx**
  - Edge 1.0+: 894 games, 52.5%, +0.3%
  - Edge 2.0+: 107 games, 52.3%, -0.1%
  - Edge 3.0+: 28 games, 46.4%

Full bucket, side and season tables are in `results/phase3b1_results.json` and `phase3b3_results.json`.

## 9. Calibration results (stated vs realized win %, 2015-2022)

| Bin | ridge8_v1 raw: games, stated, realized | ridge8_v1 calibrated (shrink) | Market no-vig |
|---|---|---|---|
| 50-52% | 421, 51.0, 52.0 | 1,145, 50.9, 50.6 | 1,891, 50.8, 50.3 |
| 52-54% | 389, 53.0, 51.2 | 662, 52.8, 51.1 | 165, 52.5, 52.1 |
| 54-56% | 319, 55.0, 49.8 | 206, 54.8, 51.5 | 2, 54.2, 50.0 |
| 56-58% | 289, 56.9, 48.1 | 39, 56.8, 56.4 | |
| 58-60% | 218, 58.9, 49.5 | 6, 58.9, 33.3 | |
| 60%+ | 422, 63.7, 52.8 | 0 | |
| **ECE** | **0.058** | **0.012** | 0.005 |

**What the table shows:**
- Calibration removes the false confidence. The 422 games where ridge8_v1 claimed 60% or more now read 50-56%, which is about what they actually won.
- **Calibration is a reporting fix, not an edge.** Calibrated ridge8_v1 is still slightly worse than the market's own probability (Brier +0.0004, CI -0.0008 to +0.0016).
- Every 3B-1 residual model calibrates to Brier 0.2501-0.2506, against 0.2500 for the market.

## 10. Weather contribution

Weather is observed at kickoff in every result here. These are not forecasts.

| Model | OOS R² | Pearson (p) | Seasons positive | Error vs base, CI | Win % | Slope p | Calibrated Brier |
|---|---:|---:|---:|---|---:|---:|---:|
| r0_wx (line + weather) | **+0.0033** | **+0.066 (0.003)** | 7 of 8 | -1.04 (-2.10 to +0.02) vs r0 | 51.1% | 0.23 | **0.2490** |
| r1_wx (ridge8 + weather) | -0.0012 | +0.031 (0.16) | 6 of 8 | -0.99 (-1.68 to -0.26) vs r1 | 51.0% | 0.85 | 0.2493 |

**What it learned:** stronger wind and rain push the result below the close, and snow pushes it slightly up. The direction matches football logic.

**Against the keep rule, r0_wx passes rules 1, 3, 4, 5 and 6 but fails rule 2:** larger weather edges do not win more often (p = 0.23).

**Why it is not a signal yet:**
1. **It uses observed weather.** The close is set with a forecast. Part of this relationship may be wind nobody forecast, which no bettor could have used.
2. **Multiple testing.** About 20 models were tested in this phase. p = 0.003 sits right at the Bonferroni line (0.0025).
3. **Its picks lean Over.** The weather model still picked Over 81% of the time, from the stale intercept. Its Under picks (402 games, mostly wind games) won 57.5%, but that is one slice, not a pre-registered test.

## 11. Injury contribution (2013+ window, Amendment A1)

| Model | OOS R² | Pearson (p) | Error vs same-window control, CI |
|---|---:|---:|---|
| r1_inj | -0.0013 | -0.025 (0.26) | +0.30 (-0.16 to +0.76), worse than r1_2013 |
| r0_inj | -0.0007 | -0.014 (0.51) | -0.04 (-0.30 to +0.20) vs r0_2013 |

**No signal.** The final injury report posts about two days before kickoff, so the closing line has absorbed it. The as-run version with 2012 data (zeros) gave the same answer.

## 12. QB contribution

| Model | OOS R² | Pearson (p) | Error vs base, CI | Slope p |
|---|---:|---:|---|---:|
| r1_qb | -0.0058 | -0.022 (0.32) | -0.18 (-0.51 to +0.15) | 0.34 |
| r0_qb | -0.0014 | +0.014 (0.54) | -0.21 (-0.74 to +0.32) | 0.15 |

**No signal.** Starter changes and backup starts are among the most-watched news in the market, and they are priced by the close.

## 13. Season stability (Pearson of predicted vs actual residual, win %)

| Season | ridge8_v1 | r1_ridge8 | r2_phase3a | r0_wx |
|---|---|---|---|---|
| 2015 | -0.008, 50.6% | -0.025, 51.0% | -0.049, 47.4% | +0.131, 48.2% |
| 2016 | -0.125, 42.4% | -0.184, 44.3% | -0.147, 52.5% | -0.099, 50.2% |
| 2017 | +0.084, 53.1% | +0.120, 46.5% | +0.071, 46.5% | +0.123, 49.6% |
| 2018 | -0.159, 48.2% | -0.009, 49.0% | -0.039, 47.4% | +0.070, 51.0% |
| 2019 | +0.058, 54.1% | -0.051, 50.2% | -0.013, 50.2% | +0.066, 53.7% |
| 2020 | +0.016, 53.4% | +0.028, 50.6% | +0.014, 50.6% | +0.076, 53.0% |
| 2021 | +0.076, 52.8% | +0.158, 46.1% | +0.141, 46.1% | +0.173, 50.9% |
| 2022 | +0.010, 52.2% | +0.036, 48.5% | +0.009, 47.0% | +0.084, 51.9% |
| Positive seasons | 5 of 8 | 4 of 8 | 4 of 8 | **7 of 8** |

**Leave-one-season-out:**
- r1 Pearson stays negative whichever season is left out (-0.051 to -0.002).
- r2 also stays negative.
- r0_wx stays positive (+0.049 to +0.087).

## 14. Ablation results

Squared-error differences are paired on the same 2,079 games, with 95% CI; negative means better.

| Comparison | Difference |
|---|---|
| r1 + form vs r1 | -0.67 (-1.08 to -0.27) |
| r1 + pace vs r1 | -0.72 (-1.36 to -0.11) |
| r1 + explosives vs r1 | -0.14 (-0.54 to +0.26) |
| r1 + possessions vs r1 | -0.14 (-0.34 to +0.08) |
| r2 all Phase 3A vs r1 | -0.78 (-1.39 to -0.18) |
| r3 conservative vs r1 | -0.01 (-0.31 to +0.30) |
| r1 (ridge8 info) vs r0 (line only) | +0.76 (+0.00 to +1.56), worse |
| r1 vs market | +1.21 (+0.14 to +2.27), worse |
| r0_wx vs market | -0.58 (-1.88 to +0.69) |

**Read this carefully.** The Phase 3A groups "improve" on r1 only because they caused heavier shrinkage toward the market, which does less damage. None of them makes the model better than the market, and none has a positive correlation with the market's error.

## 15. Which features failed

**All of these failed:**
- ridge8_v1 information
- form, pace/PROE, explosives and possessions, alone or combined
- the conservative selection
- injuries by position group
- QB change, backup and efficiency delta

The relocated-team code fix is neutral again.

**One weak lead:** observed weather, which failed the edge-vs-win test (section 10).

## 16. Does any model show genuine predictive signal?

**No model passes the keep rule.** That holds under the pre-registered calibrator choice and under logistic calibration.

| Rule | ridge8_v1 | r1 | r2 | r3 | r1_wx | r0_wx | r1_inj | r1_qb |
|---|---|---|---|---|---|---|---|---|
| 1. Residual signal (p < 0.05, R² > 0) | No | No | No | No | No | **Yes** | No | No |
| 2. Win rate rises with edge (p < 0.05) | No | No | No | No | No | No | No | No |
| 3. Brier beats ridge8_v1 and the market | No | No | No | No | Yes | Yes | No | No |
| 4. ECE at most 0.029 and 10-bin ECE improved | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| 5. Both sides, 5+ of 8 seasons | No | No | No | No | Yes | Yes | No | No |
| 6. Holds with any season left out | No | No | No | No | No | Yes | No | No |
| **Advance** | **No** | **No** | **No** | **No** | **No** | **No** | **No** | **No** |

Rule 4 is passed by every calibrated model, because calibration pulls probabilities toward 50%. That is the calibration layer working, not evidence of an edge.

## 17. Recommended model to carry forward

- **None.** No Phase 3B model is promoted, and no `phase3b_v1` is created.
- `ridge8_v1` stays the live weekly model and frozen benchmark, posting leans at 0 units, with no change to the weekly workflow.
- The calibration code stays in the repo as research tooling.

**Optional change, needs your approval:** the weekly snapshots still show ridge8_v1's raw probabilities, which overstate confidence (a "60%+" lean historically wins about 53%). A walk-forward shrink calibrator would make the displayed number honest. Because units are 0, this changes no bets. It is still a model-output change, so I have not made it.

## 18. Recommendation for Phase 3C / Phase 4

You set the stop point in advance: if the market-anchored model also failed this gate, treat that as the answer. It failed. Three phases now agree: public team statistics, possessions, injuries and QB news are already in the closing total. **My recommendation is to stop adding public football statistics and pivot.**

**Phase 4: market timing, line shopping and CLV**
1. **Line shopping first.** It is the one advantage that is measurable today and needs no model. From the per-book snapshots, measure how often the best available total or price beats consensus, and by how much.
2. **Opener-to-close movement.** Predict how the total moves from the Tuesday opener to the close, then grade every lean by CLV, not by win %. CLV is far less noisy, so it gives a verdict in hundreds of games instead of thousands.
3. **Weather as a forecast-timing hypothesis, forward only.**
   - Add a free forecast feed (for example Open-Meteo) that records wind and precipitation with each snapshot.
   - Pre-register one test: does forecast wind or rain at opener time predict the move to the close, or the residual against the close?
   - Grade it on 2026 games only, never on the holdout.
   - This is the clean version of the one lead Phase 3B found.
4. **Keep the "large-edge Under" item** (from Phase 3A) as a single forward-only hypothesis, as already planned.

**Known limits:**
- nflverse has only the close. A historical opener test needs a paid odds history.
- Otherwise the 2026 forward test is about 170 games (Weeks 7-18). That is enough to start tracking CLV, not enough to confirm a betting edge. Expect this to take more than one season.

The holdout stays sealed until an architecture is frozen.

---

## Approval needed

1. Accept the Phase 3B result: no model advances, and ridge8_v1 stays live and unchanged.
2. Open a PR to preserve the Phase 3B research code, tests and results in main. It does not touch the weekly path. Or keep the branch unmerged; your call.
3. Optional: approve a calibrated probability display for the weekly snapshots (section 17).
4. Approve the Phase 4 pivot (section 18), including adding a forecast-weather feed to the snapshot workflow.

*Not betting or financial advice. These are backtest statistics, not guarantees.*
