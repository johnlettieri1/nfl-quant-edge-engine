# Phase 5C spec: weekly spread sheet support

Drafted Nov 1 2026. Status: APPROVED by John Nov 3 2026 ("1- yes", Week 8 backfill "2-yes"). Build
amendments are in section 9.

## 1. Goal

Make the weekly "run the spread sheet" check faster and more reliable, and start measuring the one thing it
actually relies on: does taking the better side of a stale sheet line win more often than 50%?

This is line shopping plus injury checks. It is NOT a spread model. Phase 5B showed no model beats the closing
spread (PR #10). Every output is labeled that way, and units stay 0.

## 2. What changes from today's process

Today (Week 8 card): DraftKings lines are read by hand from VegasInsider, CBS, ESPN and SI. That works but is
slow and the sources disagree on timing.

With 5C:
- DraftKings, FanDuel and the other NY books come from our own spread quote files (records branch), which
  5A already collects at 2 credits per pull. 28 DraftKings rows in the Saturday snapshot alone. **No extra
  credits, no new pulls.** The check uses the latest snapshot on file.
- Opener and current come from our first-observed and last-observed snapshots, not from a website's "opened at".
- Injuries, QB status and money splits stay manual web checks (free), same as now. Nothing about injuries is
  automated or modeled.

## 3. Weekly workflow

1. John shares the sheet image (Thursday or whenever it arrives).
2. I transcribe it to a small file: game, sheet favorite, sheet line, sheet's listed week. I flag a mislabeled
   week header (as happened in Week 8) and show the transcription for a quick check.
3. Run `python run.py sheet-check --sheet <file>`. It prints, per game:
   - sheet line vs DraftKings now, vs best NY book, vs Pinnacle (reference only)
   - first-observed line and movement since
   - "sheet edge" in points and which side gets it
   - key-number flag when the sheet line sits on the better side of 3 or 7 (also 6, 10, 4, 14, noted only)
   - stale-line flag when the sheet differs from DraftKings by 1.5+ points
4. I add injury, QB, offensive line and money-split notes from free sources, one line per game.
5. Card in plain text, sheet order, same as now: one pick per game, confidence label, favorite pick, tiebreaker
   total (uses ridge8_v1's projection when that game is covered, else the market total), strongest three.
6. Thursday game first when it is on the sheet.
7. Saved to the project as `week-N-pick-card-2026.md`, as now.

## 4. Confidence labels (fixed rules, no model)

- **High:** sheet edge 1.5+ points, or the sheet line crosses 3 or 7 in our favor, and no open injury question.
- **Medium:** sheet edge 0.5 to 1.0 on a key number, or 1.0+ off a key number.
- **Low:** no sheet edge; pick follows the market side and injury notes only.
- Any game with a starting QB listed questionable or doubtful is capped at Low until his status is known.

These are labels for reading the card. They are not probabilities and carry no edge claim.

## 5. Results tracking (the part that can teach us something)

A small append-only results file for the sheet, settled from nflverse final scores (free):
- per game: sheet line, our pick, DraftKings at check time, last pregame DraftKings, final margin, cover or not
- sheet-edge games are tagged so we can answer, over time: do picks that got a stale or key-number edge cover
  more than 50%? Do High picks beat Low picks?
- Settled with the existing Tuesday settlement run. No new schedule.

What we will NOT do with it: no threshold tuning, no "best bets" from it, no promotion to a model. With ~15
games a week, a meaningful read needs a full season or more. I'll report it plainly with a confidence interval
at season end, and it will most likely be inconclusive.

Week 8 is recorded retroactively from the existing card as the first entry.

## 6. Build scope

- `src/nfl/market/sheet_check.py` (read sheet file + latest spread quotes, compute the table)
- `run.py sheet-check` command; `settle` extended to settle sheet rows
- `data/sheet/` on the records branch (append-only, same records guard)
- Unit tests: sign conventions (sheet display vs nflverse vs Odds API), key-number flags, stale flags, team
  name matching, settlement push handling
- One PR into main, not merged without John's approval

## 7. Unchanged

ridge8_v1, calibration 0.2861, frozen bookmaker groups, weather pre-registration, totals ledger, all
historical records, the 2023-2025 holdout, credit settings (2 per pull, HALT above 2, cap 60/week),
0 units.

## 8. Decisions for John

1. Approve this scope as written?
2. OK to backfill Week 8 from the saved card as the first results entry?

## 9. Amendments made during the build (Nov 3 2026)

- **A1, confidence rule wording.** Section 4 listed "crosses 3 or 7" under both High and Medium. Built as: High =
  the pick gets 1.5+ points vs DraftKings; Medium = 0.5 to 1.0 points that cross 3 or 7, or 1.0+ points that do
  not; Low = anything else; a questionable or doubtful starting QB caps the label at Low. The card stores the
  rule label (`rule_confidence`) beside the label used, so any override is visible.
- **A2, file location.** Cards go to `data/predictions/sheet/` and the ledger to `data/predictions/sheet_ledger.csv`
  (not `data/sheet/`), so the existing records guard covers them without widening its allowed paths. The ledger
  is added to the guard's append-only list and to the settle workflow's copy step. Sheet settlement runs inside
  `settle` in its own try block, so it can never fail or delay totals settlement; `settle-verify` is unchanged.
- **A3, one card per week.** A second card for the same week is refused, so no game can be settled twice.
  Corrections need John's call.
- **A4, Week 8 backfill source.** The Thursday Oct 29 morning pull ran before spread collection went live; our
  first DraftKings spread snapshot (7:45 PM ET) came after the card was made. The Week 8 card therefore records
  the DraftKings numbers documented on the saved card (`dk_source = card_web`), not a snapshot. Closing numbers
  and results come from our own snapshots and nflverse finals. Labeled `backfill = true`.
- **A5, team codes.** Sheet spellings are normalized to nflverse codes (LAR -> LA, WSH -> WAS, JAC -> JAX, ...);
  a card is refused if any row fails to match a collected game.
