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
6. **Concurrency and file layout (changed after independent review).** Each snapshot now writes its own
   new files (`data/odds/quotes/quotes_<snapshot_id>.csv`, `data/weather/forecasts/forecasts_<snapshot_id>.csv`)
   instead of appending to one shared CSV. The validation snapshot stays in the combined files it was written
   to (`quotes_v2.csv`, `forecasts_v1.csv`, untouched); readers load both. Because the weekly job and the
   planner now only ADD different files, they use separate concurrency groups (`weekly-records`,
   `pregame-records`) and a push retries with a rebase. A pregame pull can no longer displace a pending
   weekly run. Remaining edge: if a weekly run and a pregame pull run at the same moment (for example
   Sunday 9:10 AM ET with a 9:30 AM ET London game), the planner may not yet see the weekly snapshot and can
   pay for one extra pull (at most 1 credit, rare).
7. **Planner volume.** About 138 short gate runs a normal week (more in December and holiday weeks).
   The repo is public, so these cost no Actions minutes; each gate run spends no Odds API credit.
8. **Pregame prediction cadence is unchanged.** Pregame and Sunday-night pulls capture odds and weather
   only; prediction snapshots still run Thu/Sat/Sun morning.
9. **Review fixes (independent reviewer, before the PR).**
   - A pull that costs more than 1 credit writes `data/odds/raw/HALT_cost_anomaly_<ts>.json`; every later
     pull (weekly and planner) is refused until John approves removing it.
   - Copy, Guard and Commit steps run even when an earlier step failed, so anything already paid for is
     committed to records (and counted by the weekly cap).
   - The planner's pull job re-checks the plan immediately before paying: it never pays after kickoff, inside
     T-5, or for a slot another pull already covered while it waited.
   - A missing credit-balance header now refuses the pull (fail closed).
   - A failed records listing fails the planner gate instead of looking like "no snapshots yet".
   - Same-line consensus prices and probabilities are blank when fewer than 2 books post the canonical line.
   - Weather `retrieved_at` is the time the response arrived; any game whose kickoff passed during the
     request is dropped.
   - The pre-spec legacy file is no longer counted toward the weekly pull cap.

## Pre-merge safeguards (requested Oct 16 2026)

### S1. Pregame coverage is verified per game
- A game counts as covered only if a snapshot taken 10-30 minutes before THAT game's kickoff contains at
  least one valid paired two-way totals quote for it, matched by Odds API `event_id`. A snapshot that
  exists near kickoff but did not quote the game does not cover it.
- Coverage comes from the paired-quote files (`data/odds/quotes/*.csv` plus the validation file
  `quotes_v2.csv`). Those files only ever hold valid pairs: same book, same number on both sides.
- The planner gate reads them with a sparse checkout. It is still standard-library only, never downloads
  raw snapshot bodies, and never spends a credit.
- Partial book coverage counts as covered, because re-pulling does not add books that are not posting.
  `close_quality` still separately requires 5 or more books.
- Credit bounds are unchanged and now apply per game:
  - at most 2 snapshots within 40 minutes before a kickoff (any snapshot counts as an attempt, quoted or not)
  - one late attempt down to T-5, only if nothing has tried yet
  - never at or after kickoff
  - weekly cap 30, reserve 60, and any HALT file stops everything
- The pull job re-checks game by game against the latest records right before paying.

### S2. Immutable writes are crash-safe
`write_exclusive` publishes every raw snapshot, paired-quote file, weather raw file and forecast file in four steps:
1. Write the complete bytes to a hidden temp file (`.tmp-...`) in the same directory, then flush and fsync.
2. Publish with `os.link(temp, final)`. This is atomic, and it fails rather than overwrite if the final name exists.
3. On a name collision, keep the data under a `-dupN` name.
4. Remove the temp name and fsync the directory.

After a crash there is either no final file or the complete final file, never a partial one. A temp file
whose write failed is removed. One left by a hard kill is:
- never read (readers only glob published names)
- deleted by both workflows before `git add`
- rejected by the records guard

The append-only ledger is updated the same way: the full new content goes to a temp file, then `os.replace`.

Response hashes still verify. No existing record was rewritten.

### S3. Kickoff policy for conflicting or changed times
Sources:
- `odds_latest`: the commence time reported by the game's most recent Odds API snapshot
- `nflverse`: the scheduled kickoff from the current schedule (US Eastern wall clock, converted with zoneinfo)

| Case | Kickoff used | Source | Status |
|---|---|---|---|
| Both present, within 5 minutes | earlier of the two | `agreed` | resolved |
| Both present, more than 5 minutes apart | the EARLIER time | `conflict` (minutes recorded) | unresolved |
| Only one source (for example an unmatched game) | that time | `odds_api_only` / `nflverse_only` | unresolved |

Rules applied on top:
- A snapshot is pregame only if it is strictly before the game's kickoff AND strictly before the commence
  time that snapshot itself reported. An observation taken after the then-scheduled start never becomes
  pregame, even if the game is later moved.
- A legitimately postponed or flexed game keeps its valid pregame observations once both sources show the
  new time (`agreed`).
- `close_quality` is refused whenever the kickoff status is not `resolved`.
- Kickoff source, status, conflict minutes, and whether the reported time moved across snapshots
  (`kickoff_changed`) are recorded on quote rows, line-shopping rows, movement records and CLV events.

Tests cover flexed games, verified postponements, unverified postponements, unmatched games and the EDT/EST change.

### S4. Effect of the frozen 10-book universe on the research lean
- The lean uses `market_total` = the median across ALL observed books. Changing which books are observed
  changes that median, so it can change the edge (projection minus market) and, when an edge is small,
  flip the OVER/UNDER lean. This happens even though the `ridge8_v1` projection, sigma and calibration
  logic (factor 0.2861) are untouched.
- It also changes `consensus_over_prob_at_canonical`, best-line fields and line-shopping frequencies.
  Snapshots taken under the Phase 2B US-region set and under the frozen Phase 4 list are therefore not
  directly comparable.
- Measured on saved data, no credit spent. In the validation snapshot (27 games) the frozen 10-book median
  was compared with the median of only the old-list books present in that snapshot (7 of the old 9;
  BetUS and MyBookie are no longer requested):
  - the median differed for 2 games, by 0.25 points each
  - for the 14 Week 6 games with a `ridge8_v1` projection, 2 edges changed by 0.25 and no lean flipped
  - one Week 6 game (TEN @ IND) had an edge of only +0.20, so a 0.25 shift in the other direction would
    have flipped its lean
  This is a single snapshot and not a general estimate.
- Per instruction, neither the book list nor the model was changed to remove these differences. The lean
  remains a 0-unit research field.
