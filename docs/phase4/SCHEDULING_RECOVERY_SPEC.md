# Phase 4 scheduling recovery (approved Oct 21 2026)

Root cause of the Oct 17-20 miss: GitHub cron started runs 2 h 54 min to 5 h 43 min late and dropped about
94% of the 10-minute pregame runs. The old ET gate allowed only 2 hours of lateness, so every weekend run was
refused. A `workflow_dispatch` run started 19 seconds after the API call.

Design: **hybrid**. An external scheduler (cron-job.org, America/New_York) calls `workflow_dispatch` as the
primary clock. The existing GitHub crons stay as the backup. Every run, whatever started it, goes through the
same free gates, so duplicates and late runs are safe. Monitoring checks the records themselves, because a
dispatch accepted by GitHub is not proof that collection completed.

Nothing here changes ridge8_v1, calibration factor 0.2861, the sealed 2023-2025 holdout, 0 units, the frozen
bookmaker groups, the weather pre-registration or any existing record. Missing Week 6 market observations are
not backfilled.

## 1. Triggers

| Workflow | Primary (external, ET) | Backup (GitHub cron, UTC) | Spends credits |
|---|---|---|---|
| Weekly totals (`weekly.yml`) | Tue 10:11, Thu/Sat/Sun 9:11, Sun 23:31 | dual EDT/EST lines, unchanged | 1 per captured scheduled snapshot |
| Pregame planner (`pregame.yml`) | every 5 min in kickoff windows | `*/10` windows, unchanged | 1 per pull, only for an uncovered game in its window |
| Settlement (`settle.yml`, new) | Tue/Wed/Thu 10:30 | Tue/Wed/Thu 10:30 EDT and EST | never (no Odds API key in the job) |
| Collection monitor (`monitor.yml`, new) | daily 8:00 and 20:00 | 00:15 and 12:15 UTC | never |

External jobs fire one minute after each weekly target so clock skew can never land a run before its target.
Dispatch input `source: external` labels the trigger `external_dispatch`; cron runs are `github_cron`;
anything else is `manual`.

## 2. Late scheduled snapshots (`timing.scheduled_gate`)

- Each scheduled kind's window is `[T, min(T + 18 h, next scheduled target of any kind))`, where T is the
  kind's latest ET target (zoneinfo, DST automatic). Windows of different kinds never overlap, so one run
  serves at most one kind.
- Inside the window a run captures unless `(kind, T)` already has a capture in `[T, window end)`.
  Duplicates from external dispatch, both cron twins and manual runs capture at most once.
- After the window closes the run captures nothing (obsolete). A very late cron can never buy a stale
  snapshot.
- `take_snapshot` re-checks `(kind, T)` against the restored records right before paying
  (`ScheduledNotNeeded`), and refuses an obsolete window unless the trigger is `manual`.
- A target later than the current time is refused before paying (never pre-dated).
- Window ends for the current kinds: Sat 9:10 runs to Sun 3:10 (18 h cap); Sun 9:10 runs to Sun 23:30
  (next-target cap); Sun 23:30 runs to Mon 17:30; Tue 10:10 runs to Wed 4:10; Thu 9:10 runs to Fri 3:10.

### Timing provenance (never backdated)

Raw envelope and every paired-quote row carry:

| Field | Meaning |
|---|---|
| `snapshot_ts_utc` / `fetched_at` / `capture_ts_utc` | real fetch time (the file name uses it too) |
| `scheduled_target_utc` | the ET schedule target this capture served (blank for pregame/manual) |
| `schedule_delay_minutes` | real fetch time minus target |
| `delayed` | true when more than 15 minutes late |
| `trigger` | `external_dispatch`, `github_cron`, `manual`, `pregame_planner`, `validation`, `schedule` (pre-recovery records) |

In-play rows of a delayed capture are flagged `is_pregame = false` by the existing per-game kickoff rule and
never enter pregame analysis, `last_observed_pregame` or close-quality.

## 3. Pregame collection

Unchanged rules: event-id matching, paired quotes, T-30 to T-10 target window, one late attempt to T-5, never
at or after kickoff, at most 2 attempts per game, close-quality, credit preflight, per-game coverage dedupe.

New: when a scheduled full-slate snapshot is due inside a game's window (a 9:30 AM ET international game and
`sun_scheduled` at 9:10), the planner waits up to 8 minutes after that target instead of buying a second pull.
If the scheduled capture has not landed by then, it pulls as usual (still inside the window). The external
scheduler can only start the free gate; the gate decides, so no paid request happens outside a valid window.

## 4. Independent settlement (`settle.yml`)

- No Odds API request and no Odds API key anywhere in the workflow.
- Settles a game only when nflverse reports both scores AND the scheduled kickoff is at least 5 hours past.
  A missing gametime falls back to the end of the ET game day, which can only delay settlement.
- Append-only and idempotent: key `(prediction snapshot, game, variant)`; repeated runs append nothing.
- Writes only `totals_ledger_v2.csv` and `totals_ledger_legacy_pre_spec.csv`; the records guard rejects any
  rewrite of rows already recorded (Week 6 included).
- Tuesday run plus Wednesday and Thursday retries pick up late finals.

## 5. Monitoring

`src/nfl/ops/monitor.py`, run by `monitor.yml`, reads the records branch and recent runs:

| Alert | Fires when |
|---|---|
| missed-snapshot | a scheduled kind has no capture by the time its late window closed |
| missed-pregame | a kickoff slot's games have no paired quote in the 30 minutes before kickoff |
| unsettled | 36 h after kickoff, an expected settlement key (prediction snapshot file, game, variant) of a verified final is missing from its assigned ledger (legacy snapshots: `legacy_pre_spec` in the legacy ledger; Phase 4 snapshots: `consensus_all` + `best_available_ny`, `nflverse_line` or `none` in the v2 ledger). A game ID present in some ledger does not count as settled. |
| claim-unresolved | 30 min after a paid-pull claim, no raw capture carrying THAT claim's id is in records (either skipped by the final re-check, or a paid capture never reached records). Captures of other claims never count. |
| halt | a HALT file exists |
| workflow-failure | a Weekly, Pregame, Settlement or Monitor run failed in the last 26 h (covers a paid pull whose records commit failed) |
| records-stale | in season, no records commit for 50 h |

One GitHub issue per finding, label `collection-alert`, stable titles, never re-created even if closed.
Alerts start from 2026-10-22 (no alerts for the incident already handled).

Direct external alerts (healthchecks.io, not dependent on GitHub launching anything). A job that merely exits
successfully never sends a success ping; the decision is `src/nfl/ops/heartbeat.py` (unit-tested, and the
real workflow scripts are executed in tests with a stub `curl`):

| Check (slug) | Success ping only when | `/fail` when | No ping |
|---|---|---|---|
| `nfl-tue-scheduled` ... `nfl-sun-night-scheduled` | (1) this run's paid capture is committed AND its raw file is read back from the remote `records` branch, or (2) the records already hold that (kind, target) capture | any failure or abort, a refused pull, or (5) a paid capture that is not on remote `records` | no live window, obsolete window, or another run holds the claim (that run reports) |
| `nfl-pregame` | a paid capture verified on remote `records` | failure, refused pull, unverified capture | nothing to buy, or claimed by another pull |
| `nfl-settle` | settlement ran, its push succeeded (or there was nothing new), AND `settle-verify` read the ledgers back from remote `records` and found every expected key for verified finals as of the settle time | anything else | (never) |
| `nfl-monitor` | a clean monitor run | findings or error | (never) |

Expected schedules: weekly checks at each target with 1 h grace; `nfl-settle` Tue/Wed/Thu 10:30 with 2 h
grace; `nfl-monitor` 8:00 and 20:00 with 2 h grace; `nfl-pregame` at least once every 8 days. A paid capture
that cannot be verified on remote `records` is also uploaded as a 30-day run artifact
(`unverified-capture-<run>`) so the data can be recovered. Heartbeat steps do nothing when the `HC_PING_KEY`
secret is empty and never fail a job.

## 5b. Duplicate paid-pull races (Safeguard 3)

Simulated with real local git repositories (`tests/unit/test_paid_pull_races.py`, barriers force every job to
pass its records check before any job pushes):

| Scenario | Before this safeguard | Now |
|---|---|---|
| Two external weekly triggers together | serialized by the `weekly-records` group, so 1 pull if the first push succeeds | 1 pull (also proven without serialization: 5 simultaneous jobs, exactly 1 pays) |
| External + GitHub cron overlap | 1 pull if the first push succeeds | 1 pull: blocked by the claim while in progress, by the capture afterwards |
| Two pregame jobs pass checks before either commits | serialized pull jobs; 1 pull if the first push succeeds | 1 pull per game; a later job buys only games nobody claimed |
| Weekly and pregame overlapping (9:30 AM ET games) | **2 pulls possible** (different concurrency groups) | 1 pull when the scheduled snapshot claims first (the pregame job sees the in-progress scheduled claim inside the game's window). 2 pulls when the scheduled snapshot is late and the pregame pull claimed first: residual, by design (different snapshot kinds) |
| Records push failing after a paid pull | next job pays again | blocked for the claim TTL (20 min); after that a second pull is possible (the first capture is kept as an artifact) |
| Records push delayed longer than the TTL | next job pays again | a second pull is possible; the late capture then dedupes later runs |
| Claim push fails | n/a | no payment (fail closed) |
| Job crashes after claiming, before paying | n/a | claim expires after 20 min; no deadlock, no manual unlock |

Mechanism: right before paying, a job pushes a new file `data/odds/claims/claim_<time>_<kind>_r<run>.json`
to `records` without rebasing. Git accepts the push only as a fast-forward of the commit the job decided from,
so of two jobs that decided from the same records state only one claim lands. The other fetches, re-decides
and does not pay. Claims are new files only (records guard checked before every claim push); they never
change quotes, CLV or any research table.

Claim-to-capture association: the claim id is the claim file name without `.json`. The paying job passes it to
`take_snapshot`, which checks it matches the snapshot kind (before paying) and writes it into the raw envelope
as `claim_id`. A claim is resolved ONLY by a capture whose envelope carries its id and kind; an unrelated
capture of the same kind (for example another job's pregame pull for a different game) never resolves it. Its
own capture resolves it even if some targeted games or books returned no valid quote; per-game coverage from
the paired quotes then decides whether an unquoted game may be retried. Unresolved claims block for 20 minutes
and then expire. The monitor uses the same association. Captures from before this change carry no claim id and
resolve nothing; no existing record was rewritten.

**Not guaranteed: exactly-once paid collection.** Residual duplicate-credit cases: a paying job whose capture
never reaches records (then a job after the 20-min TTL may pay again); a job slower than 20 min between claim
and commit (normal is 1 to 3 min); a late scheduled snapshot and a pregame pull serving the same game window;
manual runs (a manual `kind` snapshot is never deduplicated). Each costs at most 1 extra credit and stays inside
the 30-per-week cap and the 60-credit reserve.

## 6. Credential design and GitHub App assessment

Chosen: one fine-grained personal access token, repository `johnlettieri1/nfl-quant-edge-engine` only,
repository permission **Actions: Read and write** (Metadata: Read is added automatically and cannot be
removed). No other permission. 90-day expiration, rotated by regeneration. Stored only in cron-job.org job
headers (`Authorization: Bearer ...` over HTTPS), never in GitHub code, logs, docs or a URL.

This token is **not dispatch-only**. Actions write on this repository also allows: dispatching any workflow
with any inputs (including a `manual` paid pull) on any existing branch or tag, re-running and cancelling runs,
deleting run logs, artifacts and caches, and enabling or disabling workflows. It cannot read secret values,
push code, change repository settings or touch other repositories. Paid misuse is bounded by the weekly cap
(30), the reserve (60 credits must remain after a pull) and HALT.

GitHub App (assessed, not recommended yet):

| | Fine-grained PAT | GitHub App |
|---|---|---|
| Token lifetime | up to the expiration you set (90 days here) | 1 hour per installation token |
| What is stored externally | the token | the App private key (long-lived; mints tokens until revoked) |
| Can cron-job.org use it directly | yes | no: needs a small signing service (for example a serverless function) to mint tokens |
| Actor in logs | John | the App (lets workflows treat it differently from John) |
| Tied to a person | yes | no |
| Setup and upkeep | about 10 minutes, rotate every 90 days | App + key + hosted signer to build and maintain |

Recommendation: the PAT now. Revisit an App if more people or automations need access, or if workflows should
reject paid `manual` kinds from the automation actor.

## 7. Expected credits per week

| Source | Typical | Holiday or doubleheader week |
|---|---|---|
| Scheduled snapshots (Tue, Thu, Sat, Sun, Sun night) | 5 | 5 |
| Pregame: TNF, Sun 1:00, Sun 4:05, Sun 4:25, SNF, MNF | 6 | 8 to 12 (Thanksgiving, Christmas, Saturdays, extra MNF) |
| International 9:30 AM games | 0 (served by `sun_scheduled`) | 1 if that run is late |
| Settlement, monitor, gates, /events | 0 | 0 |
| **Total** | **about 11** | **about 13 to 18** |

Hard limits (amended by Phase 5A, Oct 2026: a pull is totals + spreads = 2 credits): 60 credits per NFL week; pay only
if at least 60 credits remain AFTER the expected 2-credit pull (62 or more before it; 61 or fewer is refused; an
unknown balance is refused); HALT on any pull that costs
more than 2. A balance check made before the request cannot stop a provider that charges more than the
expected 2 credits: such a charge can take the balance below 60, and is caught afterwards from the response
headers (data kept, HALT file written, every later paid pull refused until John approves).

## 8. Known reliability limitations

- cron-job.org is a free third party. If it stops, the GitHub cron backup still runs (possibly hours late) and
  the heartbeats alert. Pregame backup crons may miss the T-30 to T-5 window entirely when GitHub is late.
- Primary and backup both run on GitHub Actions. A GitHub Actions outage stops both; heartbeats still alert.
- GitHub keeps one pending run per concurrency group; a newer pending run replaces an older one (shown as
  cancelled). This is harmless because every run re-checks the records, but it shows up in the run list.
- The missed-pregame check uses nflverse kickoff times; a kickoff moved at the last minute can be misreported.
- An expired or revoked token makes every external dispatch fail with 401. cron-job.org failure emails and the
  heartbeats report it; the GitHub cron backup keeps running.
- Paid-pull claims add a records push before each paid pull. If GitHub rejects or cannot take that push the
  pull is skipped (fail closed), which can cost a capture during a GitHub git outage.
- The heartbeat ping key can only send pings. Anyone holding it could send false "OK" pings, which is why the
  GitHub monitor also checks the records directly.

## Amendment (Oct 25 2026): kickoff-shift tolerance and monitor fixes

- **Kickoff-shift tolerance (approved by John).** The Odds API moved kickoffs by 5 minutes after we had
  captured them (Thu Oct 22: 8:15 to 8:20 PM; Sun Oct 25: three 1:00 PM games to 1:05 PM). The planner then
  measured the earlier capture against the new kickoff, found it just outside the 30-minute window and paid for
  a second pull (2 extra credits that weekend). Now a capture that was inside the 10-30 minute window for the
  kickoff reported at capture time still covers the game if the kickoff has moved by 5 minutes or less. Never
  after kickoff; a larger move (a flexed game) still needs its own capture. Both real cases were replayed
  against the saved records: no second pull.
- **Settlement alert deadline.** Settlement runs Tue/Wed/Thu, so a Thursday-night game is settled the next
  Tuesday. The old "36 h after kickoff" rule flagged it on Saturday (false alarm). A final game is now overdue
  only after the first settlement slot after kickoff + 5 h, plus 26 h for the next day's retry.
- **No self-feeding failures.** The monitor no longer lists its own failed runs as findings (a failed monitor
  run is reported by its healthchecks.io /fail ping), and a failure to open GitHub issues no longer fails the
  job. On Oct 24-25 GitHub Issues were switched off on the repository (forks start with Issues off), so issue
  creation failed, the job failed, and each next run reported the previous failure.
