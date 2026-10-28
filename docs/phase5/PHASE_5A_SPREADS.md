# Phase 5A: point-spread collection alongside totals

Approved by John Oct 25 2026 (plan: project doc `claude/phase-5-spread-plan.md`, decisions 1-3 all "yes").
Research only. Recommended units stay 0. Totals research, ridge8_v1, calibration 0.2861, the sealed 2023-2025
holdout, the frozen bookmaker groups and the weather pre-registration are unchanged.

## What changed

- **One request, two markets.** Every paid pull asks for `markets=totals,spreads` with the same frozen 10-book
  list. The Odds API charges markets x regions (10 named books = one region), so a pull costs **2 credits**.
  No new jobs, no schedule change; claims, heartbeats, monitor and records guard apply unchanged.
- **Credit limits (approved):** expected cost 2 per pull; a pull charged more than 2 writes a HALT file and stops
  all paid pulls; the weekly cap is **60 credits** (was 30 one-credit pulls), counted from what each pull
  actually cost (`x_requests_last` in its raw envelope; older 1-credit pulls count as 1); the reserve rule is
  unchanged in spirit: at least 60 credits must remain after a pull, so a pull needs **62 or more** remaining.
- **Paired spread quotes.** `data/odds/spread_quotes/spread_quotes_<snapshot_id>.csv`, one per snapshot: one
  row per (game, book) with the home team's number and price and the away team's number and price from the same
  book at the same moment; the away number must be exactly the negative of the home number. Incomplete,
  mismatched or unmappable quotes are skipped, never patched. Sign: `home_spread -3.5` = home favored by 3.5.
  Same kickoff, pregame and timing-provenance fields as totals.
- **Raw is the source of truth.** The raw envelope already stores the full response, so spread quotes can always
  be rebuilt (`spread_quotes_from_raw`). Snapshots taken before 5A have no spreads and contribute no rows.
- **Derived tables** (`python run.py phase4-build`, git-ignored): `spread_line_shopping.csv` (per snapshot and
  game, NY-actionable and all books: consensus number, best home and away number with price and book, key-number
  crossings, Pinnacle reference) and `spread_movement.csv` (first observed and last observed pregame consensus,
  move, key numbers touched, close_quality, actual home margin when final). Key numbers: 3, 7, 6, 10, 4, 14.
- **No spread CLV ledger yet.** There are no spread picks to grade until a spread model passes its pre-set test
  (Phase 5B). The data needed for it is being collected from now on.

## Expected credits

About 22 a typical week (11 pulls x 2), 26 to 36 in holiday weeks; about 95-130 a month, up to about 160 in a
holiday month, against the 500 monthly allowance.

## Validation

Fixture tests only (no paid calls): paired parsing and skips, totals rows identical with spreads present, one
request with both markets, files written and rebuilt identically from raw, HALT above 2 credits, weekly cap in
credits (29 two-credit pulls allowed, 30 refused; old one-credit pulls counted once), reserve boundary (61
refused, 62 paid), records guard, line shopping best numbers and key number 3, movement and close quality,
in-play spreads excluded. The first live pull is confirmed by its `x_requests_last = 2` header.
