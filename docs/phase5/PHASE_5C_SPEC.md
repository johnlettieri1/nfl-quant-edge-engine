# Phase 5C spec: weekly spread sheet support

Drafted Nov 1 2026. Status: DRAFT, awaiting John's approval. Nothing built yet.

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
