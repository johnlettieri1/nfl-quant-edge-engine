# NFL Totals Engine - Phase 4 Specification (APPROVED Oct 15 2026, with bookmaker amendment)

Market timing, line shopping, CLV, and forward weather.

- Audited: `main` at `b8e89abf575b7689660d61fc1a01d4ec78d88696` (verified), `records` at `df15a6e`
- Date: Oct 15, 2026
- Status: APPROVED by John Lettieri on Oct 15 2026 with one bookmaker amendment (Amendment A1 below). This file is the frozen spec; sections 1-15 are the audit/spec as proposed, and Amendment A1 overrides anything it contradicts.
- Live model unchanged: `ridge8_v1`, 0 units, calibration `ridge8_v1_shrink_cal_v1` (factor 0.2861) untouched.
- 2023-2025 holdout: sealed, not read by anything in this spec.

---

## 1. Current-state audit

### What exists

| Piece | File | What it does today |
|---|---|---|
| Odds client | `src/nfl/data/odds_api.py` | The Odds API v4. `fetch_game_odds` (paid, 1 credit for 1 market x 1 region), `fetch_events` (free), props (unused weekly). Records `fetched_at`, `x-requests-remaining`, `x-requests-used`. Does not record `x-requests-last`. |
| Snapshot writer | `src/nfl/data/odds_snapshot.py` | One API call per snapshot. Writes raw JSON to `data/odds/raw/totals_<ts>_<tag>.json`, appends per-book rows to `book_lines.csv` and per-game rows to `game_summary.csv`. Tags: `opener`, `current`, `pregame`. |
| Consensus helper | `src/nfl/data/live_odds_aggregation.py` | Per-game consensus (median line, averaged de-vig probability). Not in the weekly totals path, but has the same averaging flaw. |
| Prediction snapshot | `src/nfl/reporting/predict_totals.py` | Immutable JSON + .md per run (exclusive create, good). Pulls the latest odds row per game for `market_total`, prices, best lines. |
| Ledger / CLV | `src/nfl/reporting/ledger.py` | One row per (prediction snapshot, game) after the game finishes. `closing_line`, `closing_price`, `opening_line`, `clv_points`, `clv_price_prob`. |
| Legacy results log | `src/nfl/reporting/record_results.py` | Old moneyline/spread log. Not in the weekly path. Out of scope; not touched. |
| Automation | `.github/workflows/weekly.yml` | Cron Tue 14:10 UTC (tag `opener` + settle results), Thu/Sat/Sun 13:10 UTC (tag `current` + prediction snapshot). Writes only to `records`, with a path guard and a byte-prefix append-only check. |
| Tests | `tests/unit/test_odds_snapshot.py`, `test_ledger.py` | 3 snapshot tests, 3 ledger tests. The records guard lives only in YAML (no pytest coverage). |

### What has actually been captured

- **Exactly one live odds snapshot exists**: `20261015T180351Z`, tag `current`, taken manually at 2:03 PM ET today. 27 games (14 Week 6, 13 Week 7), 9 books, 204 book rows.
- No Tuesday `opener` snapshot has run yet. No `pregame` snapshot has ever run (nothing schedules that tag).
- `totals_ledger.csv` has **never been written** (no settled games since the stream started). This matters: we can define its schema now without breaking an append-only file.
- Credits: 499 of 500 remaining after that one call (free monthly tier).
- Books returned for `regions=us`: betmgm, betonlineag, betrivers, betus, bovada, draftkings, fanduel, lowvig, mybookieag. No Caesars, no Pinnacle.

### Timing of the current schedule (the core problem for CLV)

Cron is fixed in UTC. Today (EDT) 13:10 UTC = 9:10 AM ET. After Nov 1 (EST) it becomes 8:10 AM ET.

| Kickoff slot | Last scheduled snapshot before it | Gap |
|---|---|---|
| Thursday 8:15 PM ET | Thu 9:10 AM ET | about 11 hours |
| Sunday 1:00 PM ET | Sun 9:10 AM ET | about 4 hours |
| Sunday 4:25 PM ET | Sun 9:10 AM ET | about 7 hours |
| Sunday 8:20 PM ET | Sun 9:10 AM ET | about 11 hours |
| Monday 8:15 PM ET | Sun 9:10 AM ET | about 35 hours |

None of these is a closing line. The ledger currently calls whichever one is last the "closing" line.

---

## 2. Conceptual and data-quality issues: which are present

| # | Issue | Present? | Where / evidence |
|---|---|---|---|
| 1 | "Opener" terminology | **Yes** | Tag `opener`, `opener_summary()`, ledger column `opening_line`, workflow comments. Also a functional bug: `opener_summary()` only looks for tag `opener`, so Week 7 games first seen today (Thursday, tag `current`) would get no "opening" line, or a later Tuesday one, even though we observed them earlier. |
| 2 | "Closing line" terminology | **Yes** | `latest_pregame_summary()` docstring ("the 'closing' snapshot"), ledger `closing_line` / `closing_price` / `closing_source`. Worse: when no snapshot exists, it silently falls back to nflverse's closing total in the same column, mixing two different sources. |
| 3 | Price CLV across different totals | **Yes** | `ledger.build_row`: `price_taken` = best Over price (which sits at the best Over line, possibly a different book), `line_taken` = market median, `closing_price` = best Over price at close (possibly yet another line). `clv_price_prob` compares prices that can belong to different numbers. |
| 4 | De-vig averaged across different totals | **Yes** | `odds_snapshot.parse_totals_payload` and `live_odds_aggregation.aggregate_game_odds` average P(Over) across books regardless of line. Live example today: CHI @ ATL had books at 50.5 and 51.5; TEN @ IND ranged 43.5 to 45.0. |
| 5 | Line/price pairing | **Partly** | Per-book rows are correctly paired (same book, Over and Under at the same point, malformed pairs skipped). The break is downstream: `predict_totals._market_for` stores `market_total` = median but `over_price`/`under_price` = best-line prices from other books/lines. Also, a median over an even book count produces non-bettable numbers (49.25 for NE @ CHI today). |
| 6 | Game matching timezone | **Yes** | `match_game_id` does `kickoff - 5 hours` and a +/-1 day window. Practically robust because of the window, but it is fixed-offset arithmetic and must be replaced with `America/New_York`. |
| 7 | Provenance / hashes | **Partly** | Raw JSON keeps `fetched_at` and `requests_remaining`. Missing: content hash, code SHA, workflow run id, request parameters beyond markets/regions, `x-requests-last`, schema version. |
| 8 | Immutable / atomic writes | **Partly** | Prediction snapshots use exclusive create (good). Raw odds JSON uses `write_text` (would silently overwrite on a same-second filename collision). CSV appends are not atomic (a crash mid-write can leave a torn last row; the workflow's prefix check protects history but not a torn tail). |

Additional findings not on your list:

- **In-play rows are stored unflagged.** The Odds API `/odds` returns live in-game lines for games already started. They land in `game_summary.csv` with no flag. `latest_pregame_summary` filters them correctly, but nothing else does.
- **Ledger duplicates per game.** One row per (snapshot, game) means 3 rows per game per week (Thu/Sat/Sun). `summarize()` averages CLV across all of them, which overweights games and mixes timings.
- **`line_taken` is the median, not a bettable number.** For an execution study the "taken" number must be a real quote at a real book.
- **Kickoff changes.** Flexed or moved games change `commence_time`. Pregame status must be judged against the latest known kickoff, not the kickoff stored in that snapshot.
- **Records guard has no tests** (it is shell inside the YAML).
- **Workflow comment "ET = UTC-4 in the fall"** is wrong after Nov 1.

---

## 3. Exact files that would need modification

Modified:

| File | Change |
|---|---|
| `src/nfl/data/odds_api.py` | Capture `x-requests-last`, HTTP status, request params (never the key); optional `bookmakers=` list; credit budget guard (refuse below reserve). |
| `src/nfl/data/odds_snapshot.py` | New snapshot kinds, v2 raw envelope (hash, SHA, run id), timezone-aware matching, `minutes_to_kickoff` / `is_pregame` per row, exclusive-create raw writes, atomic CSV append, stop the cross-line de-vig average. Legacy functions `opener_summary` / `latest_pregame_summary` replaced by the definitions in section 8. |
| `src/nfl/data/live_odds_aggregation.py` | Same-line consensus de-vig only (canonical line), or mark the cross-line average as deprecated and unused. |
| `src/nfl/reporting/predict_totals.py` | `_market_for` stores paired fields only: canonical line + its consensus prices; best Over line + its own book/price; best Under line + its own book/price. No projection, edge, lean, probability, or calibration change. |
| `src/nfl/reporting/ledger.py` | New schema (`totals_ledger_v2.csv`), renamed fields, CLV definitions from section 9. Old `closing_*` / `opening_line` names retired before any row is written. |
| `run.py` | New commands: `odds --kind ...`, `pregame-check`, `weather`, `phase4-build`. |
| `.github/workflows/weekly.yml` | DST-safe dual cron + ET gate, new kind names, weather step, allow `data/weather/` in the guard, call the extracted guard script. |
| `tests/unit/test_odds_snapshot.py`, `tests/unit/test_ledger.py` | Updated for new semantics. |

New:

| File | Purpose |
|---|---|
| `.github/workflows/pregame.yml` | Pregame planner (section 7). Shares the `weekly-records` concurrency group so pushes never collide. |
| `src/nfl/ops/records_guard.py` | The guard logic moved out of YAML so it can be tested. Workflow calls it. |
| `src/nfl/data/weather_forecast.py` | Open-Meteo client and raw/parsed writers. |
| `src/nfl/data/stadiums.csv` (+ loader) | Venue id, lat/lon, roof type (outdoors / dome / retractable), surface. Unknown venue fails closed (weather `DATA UNAVAILABLE`, never guessed). |
| `src/nfl/market/timing.py` | ET conversion, kickoff resolution, pregame window logic. |
| `src/nfl/market/line_shopping.py` | Section 10 definitions. |
| `src/nfl/market/movement.py` | Section 4B movement records. |
| `src/nfl/market/clv.py` | Section 9 definitions. |
| `src/nfl/market/phase4_config.py` | Frozen constants: windows, thresholds, buckets, pre-registration id. |
| `docs/phase4/PHASE_4_SPEC.md` | This spec, as approved. |
| `docs/phase4/PREREGISTRATION_WEATHER.md` | Section 11, committed to `main` before the first weather capture so the commit SHA timestamps it. |
| `tests/unit/test_phase4_*.py` | Section 12. |

Not touched: all model code, `calibration_v1.json`, `models/total/*`, every backtest harness, all Phase 2A/3A/3B results, historical records.

---

## 4. Proposed Phase 4 data schema

Principle: raw is the source of truth and immutable; derived tables are rebuilt from raw and are **not** committed to `records`.

### Raw, immutable (committed to `records`)

**R1. `data/odds/raw/totals_<UTCts>_<kind>.json`** (envelope `odds_raw_v2`)
`schema_version, snapshot_id, snapshot_kind, trigger (schedule|pregame_planner|manual), retrieval_started_at, fetched_at, http_status, request (endpoint, markets, regions or bookmakers; key never stored), x_requests_remaining, x_requests_used, x_requests_last, code_sha, workflow_run_id, target_kickoffs (pregame only), response_sha256, response_body (exact bytes as text)`
The hash is over the exact response bytes, so it is verifiable.

**R2. `data/odds/quotes_v2.csv`** (append-only, one row per snapshot x game x book)
`snapshot_id, snapshot_ts_utc, snapshot_kind, event_id, game_id, match_method, commence_time_utc_as_reported, kickoff_et, minutes_to_kickoff, is_pregame, book, total_line, over_price, under_price, book_last_update, quote_age_minutes, raw_file, response_sha256`
Every row is one book, one line, its own two prices. Nothing else.

**R3. `data/weather/raw/forecast_<UTCts>.json`** (envelope `weather_raw_v1`)
`schema_version, snapshot_id (links to the odds snapshot of the same run), retrieval_started_at, fetched_at, provider, endpoint, params, venues_requested, response_sha256, response_body`

**R4. `data/weather/forecasts_v1.csv`** (append-only, one row per snapshot x game)
`snapshot_id, retrieved_at, game_id, venue_id, lat, lon, roof_type, weather_actionable, forecast_model, model_run_time (DATA UNAVAILABLE if the provider does not expose it), valid_start_utc, valid_end_utc, hours_ahead, temp_f_kickoff, wind_mph_kickoff, wind_mph_window_mean, wind_mph_window_max, gust_mph_window_max, precip_prob_window_max, precip_in_window_sum, snow_in_window_sum, weather_code_kickoff, humidity_kickoff, raw_file, response_sha256`
Game window = kickoff hour plus the next 3 hours (4 hourly values).

**R5. `data/predictions/totals_ledger_v2.csv`** (append-only, never written before; schema in section 9)

**Legacy files frozen:** `book_lines.csv` and `game_summary.csv` stop receiving rows at cutover (no rewrite). The one legacy raw JSON is re-parsed into the derived tables and flagged `pre_spec`.

### Derived (rebuilt by `run.py phase4-build`, output to `data/derived/phase4/`)

**D1. `line_shopping.csv`** per (snapshot, game): section 10 fields.

**D2. `movement.csv`** per game:
`game_id, season, week, kickoff_utc, kickoff_et, first_observed_ts, first_observed_median, first_observed_canonical, first_observed_books (json: book, line, prices), n_snapshots, intermediate_snapshot_ids, last_observed_pregame_ts, last_observed_pregame_minutes_to_kickoff, last_observed_pregame_median, last_observed_pregame_canonical, close_quality, move_direction (up|down|flat), move_abs_pts, max_total, max_total_ts, min_total, min_total_ts, n_line_changes (canonical line changes between consecutive snapshots), velocity_pts_per_day, dispersion_first, dispersion_last, best_over_first, best_over_last, best_over_change, best_under_first, best_under_last, best_under_change, actual_total (filled only after final)`

**D3. `clv_events.csv`** per (action snapshot S, game, side, variant): section 9 fields plus breakdown keys (timing bucket, book, dispersion bucket, edge bucket, weather flag, move direction).

**D4. `forward_dataset.csv`** per (game, snapshot): everything in your forward-dataset list. Model fields come only from the latest prediction snapshot generated **before** S (projection, edge, `raw_model_probability`, `calibrated_probability`); weather only from the row with the same `snapshot_id`.

---

## 5. Weather provider: Open-Meteo

Verified today against open-meteo.com terms and docs:

| Question | Answer |
|---|---|
| API key | Not required for the free tier |
| Terms | Free tier is **non-commercial only**. A personal research project qualifies. If this ever became a TDS or paid product, a paid plan would be needed. Data is CC-BY 4.0 (attribution). |
| Rate limits | Under 600/min, 5,000/hour, 10,000/day, 300,000/month |
| Horizon | Up to 16 days (`forecast_days` 0-16) |
| Variables | `temperature_2m, wind_speed_10m, wind_gusts_10m, precipitation_probability, precipitation, snowfall, weather_code, relative_humidity_2m` all available hourly. `wind_speed_unit=mph` supported. |
| Batching | Multiple lat/lon in one request (comma-separated) |
| US models | `best_match` blends GFS, HRRR, NBM; GFS/HRRR update hourly |
| Archives | A Historical Forecast API and Previous Runs API exist. We will **not** use them for the 2026 test (they would be reconstructed, not what we actually saw). Only forecasts we captured live count. |

Why Open-Meteo: free, no key to manage in secrets, generous limits, mph native, one batched call per snapshot, and hourly-updating US models. Alternatives (NWS api.weather.gov is also free and keyless but US-only, no batching, and has no precipitation amount in the simple hourly feed; it would fail for London/international games).

Cost: one batched request per odds snapshot covering only outdoor and retractable venues on that slate (about 10-16 locations). Roughly 15-25 requests a week. Nowhere near any limit.

Roof rules: `dome` (fixed roof, includes SoFi, Allegiant, US Bank, Ford Field, Superdome): weather recorded for audit, `weather_actionable = false`. `retractable` (ATL, ARI, DAL, HOU, IND): recorded, `actionable = conditional`, excluded from the primary weather test, reported separately. `outdoors`: actionable.

---

## 6. Expected API usage (The Odds API)

Verified rules: `/odds` costs markets x regions (we use 1 x 1 = **1 credit** per pull, regardless of how many games). `/events` and `/sports` are **free**. A `bookmakers=` list of up to 10 books costs the same as one region. Requests returning no events cost nothing.

Typical week (Weeks 7-14):

| Pull | Credits |
|---|---|
| Scheduled: Tue, Thu, Sat, Sun morning | 4 |
| Pregame slots: TNF, Sun 1:00, 4:05, 4:25, SNF, MNF (plus London 9:30 AM some weeks) | 6-8 |
| Retry allowance (at most 1 extra per slot if Actions runs late) | 0-8 |
| **Week total** | **10-20** |

Late season (Weeks 15-18, Saturday games): add 2-3 slots, so 13-26 a week. Thanksgiving and Christmas (Friday Dec 25) add 1-3.

| Period | Expected | Worst case |
|---|---|---|
| Month | about 45-90 credits | about 115 |
| Rest of 2026 regular season (about 13 weeks) | about 140-250 | about 330 |

Free tier is 500 a month. Safety rules built in: hard reserve of 60 credits (pulls refuse below it), weekly cap of 30 credits, every pull logs `x-requests-last`.

GitHub Actions minutes: the planner runs every 10 minutes in game windows, about 130-160 short runs a week (no package install, about 20-40 seconds each, billed as 1 minute each), so about 600-700 minutes a month. If the repo is public (it appears to be, given the `docs/` site), Actions minutes are free. If private, that fits inside the 2,000 free minutes but is not trivial; a leaner targeted-cron version can cut it to about 30 runs a week. **Please confirm repo visibility** (my session could clone it but not query its settings).

---

## 7. Proposed pregame snapshot schedule

GitHub cron runs in UTC only, supports every 5 minutes at best, and scheduled runs are routinely delayed (often 5-20 minutes, occasionally dropped under load). The design accepts that and labels what actually happened.

**A. Scheduled snapshots (keep, fix DST).** Each gets two cron lines (EDT hour and EST hour). A Python gate checks the `America/New_York` local time and exits without spending a credit if it is the wrong one.

| Kind | ET target | Purpose |
|---|---|---|
| `tue_scheduled` | Tue 10:10 AM | Usually `first_observed` for next week's games; settle last week |
| `thu_scheduled` | Thu 9:10 AM | Intermediate |
| `sat_scheduled` | Sat 9:10 AM | Intermediate |
| `sun_scheduled` | Sun 9:10 AM | Intermediate |

Optional (+1 credit a week, recommended): `sun_night_scheduled` at Sun 11:30 PM ET to get closer to the true open for the next week. Books typically post next week's totals Sunday night.

**B. Pregame planner (new `pregame.yml`).**
1. Runs every 10 minutes during UTC windows that cover kickoffs in both EDT and EST (Thu/Sun/Mon nights, Sunday 9 AM-9 PM ET, Saturdays and holidays late season).
2. Calls the **free** `/events` endpoint to get current kickoff times.
3. If any game kicks off between 5 and 35 minutes from now, and no `pregame_window` snapshot already covers it inside the 10-30 minute target, it makes one `/odds` pull (1 credit) plus the weather call. The pull returns the whole slate, so other games get free intermediate observations.
4. Target is T-20. If the first attempt landed outside 10-30 minutes because of delay, one retry is allowed up to T-5. Maximum 2 pulls per kickoff slot. Never after kickoff.
5. Kickoffs within 15 minutes of each other (4:05 and 4:25) share a pull when one pull lands in both windows.
6. Budget guard and weekly cap from section 6.

Even when it works perfectly, the label stays `last_observed_pregame`, with `close_quality = true` only under the rule in section 8.

---

## 8. Terminology

Stored at capture (what we did): `snapshot_kind` = `tue_scheduled | thu_scheduled | sat_scheduled | sun_scheduled | sun_night_scheduled | pregame_window | manual`. Legacy `current` maps to `legacy_current` (only one file, today's).

Computed per game (what it means), never stored as a tag:

| Term | Exact definition |
|---|---|
| `first_observed` | Earliest snapshot containing the game with at least 1 valid book quote. Immutable once a later snapshot exists; recomputation can never move it later because raw files are append-only. Never called "opener". |
| `current` | Latest snapshot so far for a game that has not kicked off. Live display only. |
| `last_observed_pregame` | Latest snapshot with `snapshot_ts < kickoff`, where kickoff = latest known kickoff (final nflverse schedule if available, else the most recent `/events` or snapshot `commence_time`). Snapshots at or after kickoff are excluded. |
| `close_quality` | Flag on `last_observed_pregame`: true only if taken 0-30 minutes before kickoff (strictly before) and at least 5 books quote the game. Otherwise false, and reports show the actual minutes, e.g. "last observed pregame, T-4h10m". |
| `true_opener` | Not used. We have no verified market-open data. |
| `closing_line` | Not used anywhere in Phase 4 output. |

---

## 9. CLV definitions

Notation: action snapshot S, reference snapshot R = `last_observed_pregame` for the same game, with S strictly before R. If S is R, no CLV row (no action before the reference).

**Canonical line** at a snapshot = the most common posted line among books; ties broken by closest to the median, then the lower number. Always a real posted number (unlike the median, which can be 49.25).

**What was "taken" at S** (two variants, always reported separately):
- `best_available`: OVER takes the lowest line at S, UNDER the highest; tie on line broken by best price; record the book and that book's own price at that line.
- `consensus`: the canonical line at S, priced at the median price among books posting exactly that line.

**Line CLV** (reference = canonical line at R; median at R reported as a secondary column):
- OVER: `line_clv = ref_line_R - line_taken`
- UNDER: `line_clv = line_taken - ref_line_R`
- Positive = the taken number beat the later market.

**Price CLV** (only when numbers match; otherwise blank):
- `price_clv_same_book`: if the taken book posts the exact same line at R: `implied(ref price at R) - implied(price_taken)`, raw implied probabilities, same side. Positive = our price was better.
- `price_clv_same_line_consensus`: if 3+ books post exactly `line_taken` at R: same formula against the median price at that line. Blank otherwise.
- `fair_ev_vs_ref` (informational): `p_fair_R x decimal(price_taken) - 1`, where `p_fair_R` = de-vigged probability for the side, averaged only across books posting exactly `line_taken` at R (3+ books required). Blank otherwise.
- No combined line+price metric. Not computed until a conversion is explicitly modeled and validated.

**Ledger v2 columns:** `game_id, season, week, away_team, home_team, action_snapshot_id, action_ts, minutes_to_kickoff_at_action, side, variant, line_taken, price_taken, book_taken, ref_snapshot_id, ref_ts, ref_minutes_to_kickoff, ref_close_quality, ref_canonical_line, ref_median_line, line_clv, line_clv_vs_median, price_clv_same_book, price_clv_same_line_consensus, fair_ev_vs_ref, first_observed_line, model_version, projected_total, model_edge, raw_model_probability, calibrated_probability, actual_total, result, units_risked (always 0), recorded_at, schema_version`

**Tracked "sides"** (all hypothetical, 0 units):
1. Mechanical OVER and UNDER at every snapshot before R (note: consensus line CLV is zero-sum between Over and Under by construction; only shopping and timing can make both positive).
2. `ridge8_v1` lean side (a hypothesis to test, not an assumption).
3. Pre-registered weather Under (section 11).

**Reported breakdowns:** mean, median, % > 0, % >= +0.5, % >= +1.0, split by side, timing bucket (Tue / Thu / Sat / Sun morning / pregame), book, dispersion bucket (range 0, 0.5, 1.0+), ridge8 |edge| bucket (<1, 1-2, 2-3, 3+; matches your project tiers, fixed now), weather flag, and move direction. One row per game per variant per timing bucket is used for averages (fixes the duplicate-row overweighting).

---

## 10. Line-shopping definitions

Per (game, snapshot), using only books with a valid paired two-way quote in that snapshot:

| Field | Definition |
|---|---|
| `n_books` | Count of valid quotes |
| `median_total` | Median of book lines |
| `canonical_total` | Section 9 rule |
| `best_over_total` / `_price` / `_books` | Lowest line; best Over price among books at that line; all books tied |
| `best_under_total` / `_price` / `_books` | Highest line; best Under price at that line |
| `worst_over_total`, `worst_under_total` | Highest line, lowest line |
| `over_line_adv_pts` | `canonical_total - best_over_total` (>= 0) |
| `under_line_adv_pts` | `best_under_total - canonical_total` (>= 0) |
| `over_line_adv_vs_median`, `under_...` | Same, against the median |
| `consensus_over_price_at_canonical` | Median Over price among books at exactly the canonical line (same for Under) |
| `best_over_price_at_canonical` | Best Over price at exactly the canonical line |
| `price_adv_cents_over` | Best minus consensus at the canonical line, in cents on a continuous scale (American `a` maps to `a` if `a <= -100`, else `a - 200`; so -110 to +105 = 15 cents) |
| `price_adv_prob_over` | `implied(consensus price) - implied(best price)` at the canonical line |
| Price fields | Blank if fewer than 2 books post the canonical line. Never compared across lines. |
| `line_range`, `line_std`, `n_distinct_lines` | Dispersion |
| `crosses_key_number` | Best vs canonical straddles 37, 41, 43, 44, 47, 51 (fixed list now, descriptive only) |
| `stale_quote_flags` | Books whose `book_last_update` is over 6 hours old (recorded, not excluded) |

Gate B summary: % of (game, snapshot) rows with `over_line_adv_pts` (and Under separately) >= 0.5, >= 1.0, >= 1.5; distribution of `price_adv_cents` at identical lines; all split by timing bucket and by book group (below).

**Book groups:** frozen by Amendment A1 below (NY-actionable, Reference/Sharp, Other/Offshore). Every Gate B and Gate C number is reported three ways: NY-actionable books only, all observed books, and against the reference/sharp book where available. Offshore and reference books are research-only; nothing in Phase 4 recommends placing a wager at them.

---

## 11. Weather hypothesis and pre-registered thresholds

To be committed to `main` as `docs/phase4/PREREGISTRATION_WEATHER.md` **before** the first weather capture. No threshold may be added or changed after 2026 outcomes are seen; any change requires a new pre-registration with a later start date, evaluated only on data after that date.

**Population (primary):** 2026 regular-season games, `roof_type = outdoors`, with a weather capture from the same run as an odds snapshot taken 24-120 hours before kickoff (the Tue/Thu/Sat window). If several qualify, the **earliest** one is used. Retractable and dome games excluded from the primary test (reported descriptively).

**Weather measure:** `wind_mph_window_mean` (mean sustained 10 m wind over kickoff hour + next 3 hours) and precipitation from that same capture.

**Flags (fixed now):**
- `W15`: `wind_mph_window_mean >= 15`
- `W20`: `wind_mph_window_mean >= 20` (subset of W15, reported as a tier)
- `WET`: `precip_prob_window_max >= 60` AND `precip_in_window_sum >= 0.10`
- `EXTREME`: `W20` OR `snow_in_window_sum >= 1.0`

**H-W1 (primary):** For W15 games, market movement from the weather snapshot S to `last_observed_pregame` (canonical line) is more negative than for non-W15 outdoor games, and an UNDER taken at S (`best_available` variant, NY-licensed books) has mean line CLV > 0.

**H-W2 (secondary):** Same two statements for WET games.

**Test:** one-sided difference in mean movement (W15 vs other outdoor) with a permutation test, and mean Under line CLV with a bootstrap 95% CI. Reported regardless of outcome. Honest expectation: the rest of 2026 will produce only roughly 15-35 W15 outdoor games, so this is likely underpowered and will read as "descriptive, not conclusive" for 2026, with the same pre-registration rolling into 2027.

**Model hypothesis H-M1 (descriptive only):** larger ridge8 |edge| buckets have higher mean line CLV for the lean side. Buckets fixed above.

---

## 12. Tests to add

| Test | Checks |
|---|---|
| `test_line_price_paired_same_book` | Every stored price belongs to the same book and line as its `total_line`; malformed two-ways dropped |
| `test_best_over_lowest_then_price` | Best Over = lowest line, then best price at that line |
| `test_best_under_highest_then_price` | Best Under = highest line, then best price at that line |
| `test_price_clv_blank_when_lines_differ` | All price CLV fields blank on any line mismatch |
| `test_line_clv_sign_over_under` | Over: ref - taken; Under: taken - ref; worked examples |
| `test_canonical_line_rule` | Mode, tie to median, then lower; never a non-posted number |
| `test_consensus_devig_same_line_only` | No averaging across different totals |
| `test_first_observed_never_overwritten` | Adding later snapshots never changes first_observed; earlier-dated legacy file is honored |
| `test_last_pregame_only_before_kickoff` | Snapshots at or after kickoff excluded |
| `test_post_kickoff_never_becomes_reference` | Including a moved/flexed kickoff case |
| `test_close_quality_rule` | True only at 0-30 min with 5+ books |
| `test_timezone_est_edt` | Matching and gates correct on both sides of Nov 1, 2026 and the March DST change; no fixed offsets |
| `test_dual_cron_gate` | Wrong-offset cron run exits with zero credits spent |
| `test_weather_retrieved_before_kickoff` | Weather `retrieved_at < kickoff` and within the same run as its odds snapshot |
| `test_weather_never_backfills` | A weather row only joins its own `snapshot_id`; never earlier snapshots |
| `test_indoor_flagged` | Dome = non-actionable, retractable = conditional, unknown venue = DATA UNAVAILABLE |
| `test_in_play_rows_flagged` | Post-kickoff rows marked `is_pregame = false` |
| `test_budget_guard` | Pull refuses below reserve and over weekly cap |
| `test_raw_exclusive_create_and_hash` | Raw files cannot be overwritten; stored hash matches bytes |
| `test_holdout_sealed_phase4` | Phase 4 modules load only season >= 2026; asserting on any 2023-2025 row; no import of harness loaders |
| `test_calibration_frozen` | `calibration_v1.json` hash and factor 0.2861 unchanged |
| `test_records_guard_blocks_source` | Guard rejects any path outside `data/(odds|predictions|weather)/` |
| `test_records_guard_append_only` | Guard rejects modified/deleted immutable files and rewritten append-only CSVs |

---

## 13. Risks and limitations

1. **No true opener.** Tuesday 10 AM is usually 36+ hours after lines open, and much of the sharp move happens early. Our movement numbers will understate total movement. (The optional Sunday-night pull narrows this.)
2. **`last_observed_pregame` is not a true close,** even at T-20. Late steam in the last 10-20 minutes is missed. Mean CLV against it is biased toward zero.
3. **Actions scheduling is best-effort.** Some slots will land late or not at all. Labeled honestly; never relabeled as close.
4. **Small sample.** About 12 weeks x 14-16 games = roughly 170-190 games. Enough for line-shopping frequencies and a rough overall mean CLV (standard error around 0.1 point). Not enough for subgroup claims, the movement model (Gate D), or the weather test (Gate E). Those likely extend into 2027.
5. **Available is not bettable.** A best line at one book may have low limits or be stale. Mitigated by the NY-only split and stale-quote flags, not eliminated.
6. **Book mix changes the consensus.** Switching book lists changes medians. Doing it now (one snapshot in) costs almost nothing; doing it later breaks comparability.
7. **Weather resolution.** Forecasts are for a grid cell, not the inside of a stadium bowl. Retractable-roof decisions are unknown before kickoff.
8. **Open-Meteo non-commercial clause.** Fine for personal research; would need a paid plan for any commercial use.
9. **Credits.** Free tier is 500 a month; guarded, but a misconfigured workflow could burn credits. Weekly cap and reserve limit the damage.
10. **Forward window.** Your registered forward test is Weeks 7-18. Realistically, approval, build, tests and a manual run mean the first full Phase 4 week is likely Week 8. Weeks 6-7 data is kept, used for line shopping, and flagged `pre_spec`.

---

## 14. Recommended implementation plan

On a new branch `phase-4-market-clv` from `main` at `b8e89ab`. No push to `main`, no rewrite of `records`.

1. Commit this spec and the weather pre-registration to `docs/phase4/` (first commit on the branch, so the pre-registration SHA predates any weather data).
2. Terminology and integrity fixes: kinds, v2 raw envelope with hash/SHA, exclusive-create raw writes, atomic CSV append, timezone matching, paired market fields in `predict_totals`, ledger v2 schema.
3. Book list change (if you approve) and credit guard.
4. Weather capture (Open-Meteo client, `stadiums.csv`, raw + parsed writers).
5. Timezone-safe scheduling: dual-cron gate in `weekly.yml`, new `pregame.yml` planner, guard extracted to `src/nfl/ops/records_guard.py`, `data/weather/` allowed.
6. Derived line-shopping table (D1).
7. Derived movement and CLV tables (D2, D3, D4).
8. Full test suite from section 12, plus the existing suite and leakage tests passing; Phase 2A baseline reproduction re-run to prove nothing moved.
9. One manual live snapshot (1 credit + 1 weather call) via `workflow_dispatch`.
10. Verify `records` behavior: new files appended, legacy files untouched, guard passes, nothing outside allowed paths.
11. Open the PR for your review. Forward collection starts only after you merge.

Reports (Gate B first, then C) come as a separate research PR once enough weeks settle.

---

## 15. Holdout confirmation

2023-2025 stays sealed. Nothing in Phase 4 reads it: all Phase 4 evidence comes from the 2026 live snapshot stream and 2026 final scores. It is not used for CLV, movement modeling, calibration, threshold selection, weather testing, validation, or feature selection. A new test (`test_holdout_sealed_phase4`) enforces this, and the existing holdout assertions in the Phase 2A/3A/3B harnesses are untouched. The display calibration (factor 0.2861, seasons 2001-2022) is not refit or changed, and a hash test pins it.

---

## Approved decisions (Oct 15 2026)

1. Spec and architecture approved.
2. Sunday 11:30 PM ET scheduled snapshot approved (`sun_night_scheduled`).
3. Repository is PUBLIC: full pregame planner (every 10 minutes in game windows), not the lean private-repo version.
4. Open-Meteo approved for forward-only weather capture under the non-commercial personal-research use case.
5. 2023-2025 holdout stays sealed.
6. `ridge8_v1` stays at 0 units.
7. Calibration factor 0.2861 stays frozen and unchanged.

Out of scope (explicit): no line-movement machine-learning model, no betting thresholds, no units, no official bets, no use of 2023-2025, no modification of historical snapshots, no relabeling of old snapshots as true openers or closes, and `last_observed_pregame` is never called a true closing line.

---

## Amendment A1: bookmaker availability probe and frozen book groups

### A1.1 Probe (run Oct 15 2026, John's current Odds API subscription)

- Method: one `/v4/sports/americanfootball_nfl/odds` request, `markets=totals`, `oddsFormat=american`, `bookmakers=draftkings,fanduel,betmgm,betrivers,pinnacle,betonlineag,lowvig,espnbet,williamhill_us,fanatics`. Run inside GitHub Actions (the API host is not reachable from the development environment).
- Cost structure: `x-requests-last = 1` for 10 named books spanning the `us`, `us2` and `eu` regions. Matches the approved plan (1 credit per totals pull for up to 10 books). No material difference.
- Credits actually spent on the probe: **2**. Attempt 1 made the paid call but crashed before saving its result (the output folder did not exist on the fresh checkout; a bug in the probe script, not an API issue). A free `/sports` pre-check in attempt 2 confirmed attempt 1 had spent its credit (498 remaining before attempt 2). Attempt 2 succeeded; its result was captured through job annotations (the raw body was not saved, same folder bug, fixed). Balance after the probe: **497 of 500**.
- Full record: `docs/phase4/probe/BOOKMAKER_PROBE.md`.

| Book key | Brand | Events with NFL totals returned (of 27 listed) | Returned? |
|---|---|---:|---|
| draftkings | DraftKings | 27 | Yes |
| fanduel | FanDuel | 27 | Yes |
| betmgm | BetMGM | 14 | Yes (Week 6 only at probe time) |
| betrivers | BetRivers (Rush Street Interactive) | 27 | Yes |
| espnbet | theScore Bet (formerly ESPN BET) | 27 | Yes |
| pinnacle | Pinnacle | 14 | Yes (Week 6 only at probe time; Odds API notes Pinnacle odds come from its public site and may be delayed) |
| betonlineag | BetOnline.ag | 27 | Yes |
| lowvig | LowVig.ag | 27 | Yes |
| williamhill_us | Caesars | 0 | **No** (Odds API lists it as paid-subscription only) |
| fanatics | Fanatics | 0 | **No** (Odds API lists it as paid-subscription only) |

Caesars and Fanatics are not available on the current plan. Per John's instruction, nothing is upgraded or purchased; this is documented as a limitation.

### A1.2 New York license status (source: NY State Gaming Commission sports wagering page, checked Oct 15 2026)

Licensed mobile operators listed: Bally Bet, BetMGM, Caesars Sportsbook, DraftKings Sportsbook, theScore Bet, Fanatics Sportsbook, FanDuel Sportsbook, Resorts World Bet, Rush Street Interactive (BetRivers). Pinnacle, BetOnline, LowVig, Bovada, BetUS and MyBookie are not licensed in New York. (This is a data-labeling rule, not legal advice.)

### A1.3 Frozen book groups

| Group | Books | Use |
|---|---|---|
| **A. NY-ACTIONABLE** | draftkings, fanduel, betmgm, betrivers, espnbet (theScore Bet); plus ballybet (provisional, see A1.4) | Returned on this plan AND NY-licensed. The only group used for "available to John" line-shopping and CLV numbers. |
| **B. REFERENCE / SHARP** | pinnacle | Market reference and CLV research only. Never presented as a book John can legally use in NY. |
| **C. OTHER / OFFSHORE** | betonlineag, lowvig; plus bovada (provisional, see A1.4) | Returned on this plan, not NY-actionable. Research only. Never recommended for wagering. |
| Not available on plan | williamhill_us (Caesars), fanatics | Documented limitation. Re-test only if the subscription changes. |
| Legacy-only | betus, mybookieag | Present only in the single pre-spec snapshot of Oct 15 2026 (`legacy_current`). Classified OTHER/OFFSHORE when that snapshot is re-parsed. |

Group membership is fixed by license status and is stored in `configs/phase4_books.yaml`. A book contributes to a group's numbers in a given snapshot only if it actually returned a valid paired quote in that snapshot.

### A1.4 Frozen request list (1 credit per pull)

`bookmakers=draftkings,fanduel,betmgm,betrivers,espnbet,ballybet,pinnacle,betonlineag,lowvig,bovada`

The probe verified 8 books and freed 2 of the 10 slots (Caesars and Fanatics are unavailable). Those slots go to:

- `ballybet` (Bally Bet): NY-licensed, listed by the Odds API as a free `us2` book. Availability not yet verified. It is verified by the Phase 4 validation pull at zero extra credit cost (cost is per 10 books, not per book). If it does not return quotes, the slot simply produces no rows; the request list is not changed without John's approval.
- `bovada` (Bovada): offshore, research only. Kept for continuity with the pre-spec Oct 15 snapshot, which included it.

The request list, the groups, and the license labels may only change with John's explicit approval, recorded as a new amendment.

### A1.5 Reporting rule

Every line-shopping (Gate B) and CLV (Gate C) table shows three blocks: (1) NY-actionable only, (2) all observed books, (3) reference/sharp comparison where Pinnacle quoted the game. Pinnacle coverage is partial (it posts later in the week), so block (3) reports its own sample size.

### A1.6 Other amendments

- All live Odds API and Open-Meteo calls run inside GitHub Actions; the development environment cannot reach either host.
- Credit accounting updated: 3 credits used before Phase 4 collection (1 pre-spec snapshot, 2 probe). 497 remaining at amendment time.
- Limitations added: Caesars and Fanatics absent (two of the largest NY books), so NY-actionable best-line frequencies are a lower bound on what a NY bettor with all nine licensed books could find; Resorts World Bet has no Odds API key; Pinnacle quotes are delayed and partial.
