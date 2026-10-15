# Phase 4 bookmaker availability probe (Oct 15 2026)

One-shot probe of John's current The Odds API subscription for the NFL totals market.

## Request

- Endpoint: `GET /v4/sports/americanfootball_nfl/odds`
- Params: `markets=totals`, `oddsFormat=american`,
  `bookmakers=draftkings,fanduel,betmgm,betrivers,pinnacle,betonlineag,lowvig,espnbet,williamhill_us,fanatics`
- Runner: GitHub Actions, workflow `.github/workflows/phase4-bookmaker-probe.yml` on branch
  `phase-4-market-clv` (removed from the branch after the result was captured).

## Attempts and credits

| Attempt | Commit | Paid call made? | Result saved? | Evidence |
|---|---|---|---|---|
| 1 | bb34905 | Yes (1 credit) | No: crashed writing the output file (folder `docs/phase4/probe/` did not exist on the fresh checkout) | Free `/sports` pre-check in attempt 2 showed 498 remaining (was 499) |
| 2 | 6e14127 | Yes (1 credit) | Summary captured via job annotations (check run 116704695870); raw body not saved (same folder bug) | Annotations quoted below |

Total probe cost: **2 credits**. Balance after: **497 remaining, 3 used** this cycle.

## Annotations from attempt 2 (verbatim)

```
probe  stage=start
probe  free /sports pre-check status=200 remaining=498 used=2
probe  paid /odds status=200 x-requests-last=1 remaining=497 used=3
probe  per_book={"draftkings": 27, "fanduel": 27, "betmgm": 14, "betrivers": 27, "pinnacle": 14, "betonlineag": 27, "lowvig": 27, "espnbet": 27, "williamhill_us": 0, "fanatics": 0} unexpected=[]
```

(`per_book` = number of events for which that book returned an NFL totals market. 27 events were listed: 14 Week 6, 13 Week 7.)

## Conclusions

- Cost: 1 credit for 10 named books across `us`, `us2`, `eu`. Matches the approved plan.
- Returned: draftkings, fanduel, betmgm, betrivers, espnbet (theScore Bet), pinnacle, betonlineag, lowvig.
- Not returned: williamhill_us (Caesars), fanatics. Consistent with the Odds API marking both as paid-subscription only. Not upgraded, per instruction.
- Pinnacle: returned (Week 6 games only at probe time).
- Frozen groups: see `docs/phase4/PHASE_4_SPEC.md`, Amendment A1, and `configs/phase4_books.yaml`.
