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
| unsettled | a predicted, final game is in neither ledger 36 h after kickoff |
| halt | a HALT file exists |
| workflow-failure | a Weekly, Pregame, Settlement or Monitor run failed in the last 26 h (covers a paid pull whose records commit failed) |
| records-stale | in season, no records commit for 50 h |

One GitHub issue per finding, label `collection-alert`, stable titles, never re-created even if closed.
Alerts start from 2026-10-22 (no alerts for the incident already handled).

Direct external alerts (healthchecks.io, not dependent on GitHub launching anything):

| Check (slug) | Pinged by | Expected |
|---|---|---|
| `nfl-tue-scheduled`, `nfl-thu-scheduled`, `nfl-sat-scheduled`, `nfl-sun-scheduled`, `nfl-sun-night-scheduled` | Weekly run that served that window (captured, or found it already captured) | each target, 1 h grace |
| `nfl-settle` | Settlement run | Tue/Wed/Thu 10:30, 2 h grace |
| `nfl-monitor` | Monitor run: success only when clean, `/fail` on findings or error | 8:00 and 20:00, 2 h grace |
| `nfl-pregame` | Pregame pull job (success or `/fail`) | at least once every 8 days |

Every failed job pings `/fail`. Heartbeat steps do nothing when the `HC_PING_KEY` secret is empty and never
fail a job.

## 6. Credential design and GitHub App assessment

Chosen: one fine-grained personal access token, repository `johnlettieri1/nfl-quant-edge-engine` only,
repository permission **Actions: Read and write** (Metadata: Read is added automatically and cannot be
removed). No other permission. 90-day expiration, rotated by regeneration. Stored only in cron-job.org job
headers (`Authorization: Bearer ...` over HTTPS), never in GitHub code, logs, docs or a URL.

This token is **not dispatch-only**. Actions write on this repository also allows: dispatching any workflow
with any inputs (including a `manual` paid pull) on any existing branch or tag, re-running and cancelling runs,
deleting run logs, artifacts and caches, and enabling or disabling workflows. It cannot read secret values,
push code, change repository settings or touch other repositories. Paid misuse is bounded by the weekly cap
(30), the reserve (60) and HALT.

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

Hard limits stay: 30 per NFL week, refuse below 60 remaining, HALT on any pull that costs more than 1.

## 8. Known reliability limitations

- cron-job.org is a free third party. If it stops, the GitHub cron backup still runs (possibly hours late) and
  the heartbeats alert. Pregame backup crons may miss the T-30 to T-5 window entirely when GitHub is late.
- Primary and backup both run on GitHub Actions. A GitHub Actions outage stops both; heartbeats still alert.
- GitHub keeps one pending run per concurrency group; a newer pending run replaces an older one (shown as
  cancelled). This is harmless because every run re-checks the records, but it shows up in the run list.
- The missed-pregame check uses nflverse kickoff times; a kickoff moved at the last minute can be misreported.
- An expired or revoked token makes every external dispatch fail with 401. cron-job.org failure emails and the
  heartbeats report it; the GitHub cron backup keeps running.
- The heartbeat ping key can only send pings. Anyone holding it could send false "OK" pings, which is why the
  GitHub monitor also checks the records directly.
