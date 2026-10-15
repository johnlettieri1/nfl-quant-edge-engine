# Phase 4: live validation pull and implementation notes

## Live validation pull (Oct 15 2026, 22:54 UTC)

Run by a one-shot workflow on `phase-4-market-clv` (commit 7f6a79c, removed afterwards), using the branch
code, the frozen 10-book list, and the records guard.

| Check | Result |
|---|---|
| Snapshot id | `20261015T225438Z_manual` (kind `manual`, trigger `validation`) |
| Credit cost | `x-requests-last = 1`; free preflight balance 497, after 496 |
| Books returned | all 10: draftkings, fanduel, betmgm, betrivers, espnbet, **ballybet**, pinnacle, betonlineag, lowvig, bovada |
| Coverage | 27 events; 231 paired quotes; betmgm, bovada, pinnacle quoted Week 6 only (14 events) |
| Matching | 0 unmatched games (team + US Eastern date), 0 in-play rows |
| Raw envelope | `odds_raw_v2`; SHA-256 of response bytes verifies; no API key anywhere in the file |
| Weather | 18 venues in one Open-Meteo call; 23 rows: 13 ok, 3 dome (not requested), 7 beyond the forecast horizon (labeled DATA UNAVAILABLE); 2 W15 outdoor games (Week 6, descriptive only) |
| Records branch | `df15a6e` -> `42ddea2`: 4 files ADDED (quotes_v2.csv, raw snapshot, forecasts_v1.csv, weather raw). The 4 pre-existing files are byte-identical (verified by SHA-256). Guard passed. |
| Derived tables | rebuilt from raw: 2 snapshots, 27 games, 14 rows with a Pinnacle reference |

Ballybet (A1.4 provisional slot) is confirmed: NY-ACTIONABLE now has 6 returned books.
Credit balance after all pre-collection work: **496 of 500** (1 pre-spec snapshot, 2 probe, 1 validation).

## Implementation clarifications (for review; none change an approved rule)

1. **Line-shopping scopes.** `ny_*_line_adv_pts` measures shopping value inside the NY-actionable scope
   (best NY number vs the NY canonical line); `all_*` does the same across all observed books. CLV always
   uses the ALL-OBSERVED canonical line at `last_observed_pregame` as the reference, with the NY canonical
   and Pinnacle lines reported as extra columns.
2. **Model fields in the forward dataset.** A prediction snapshot is generated a few minutes AFTER the odds
   snapshot it uses, so "latest prediction before S" would always pick a stale one. Rule used: the
   prediction built from snapshot S's own market if one exists, else the latest prediction generated
   before S. Never a prediction built from a later snapshot.
3. **Weather horizon.** Open-Meteo counts `forecast_days` from 00:00 GMT of the request day. The
   validation pull showed games 8-10 days out labeled DATA UNAVAILABLE under a 10-day setting, so the
   setting is now 16 days (the free maximum, still one call) and the horizon check matches the API.
   This affects descriptive rows only; the pre-registered window is 24-120 hours.
4. **The prediction market total stays the median of all observed books** (same basis as Phase 2B), so
   `ridge8_v1` projection, edge, lean and calibration logic are unchanged. The median now spans the
   frozen 10-book list. All unpaired price fields were removed from odds-sourced snapshots.
5. **Legacy v1 ledger.** Until this PR merges, `main`'s Tuesday run will still create the old
   `totals_ledger.csv` for the Oct 15 Week 6 prediction snapshot. Phase 4 writes only
   `totals_ledger_v2.csv`; the v1 file is left untouched (append-only) and is not used.
6. **Concurrency.** The planner's pull job shares the `weekly-records` group with the weekly job so
   records pushes never collide. GitHub keeps at most one pending run per group, so in a rare overlap
   (planner pull running, weekly pending, a second planner pull queued) the pending weekly run can be
   replaced. The gate job is outside the group, so this needs two pulls in a row while a weekly run waits.
7. **Planner volume.** About 138 short gate runs a normal week (more in December and holiday weeks).
   The repo is public, so these cost no Actions minutes; each gate run spends no Odds API credit.
8. **Pregame prediction cadence is unchanged.** Pregame and Sunday-night pulls capture odds and weather
   only; prediction snapshots still run Thu/Sat/Sun morning.
