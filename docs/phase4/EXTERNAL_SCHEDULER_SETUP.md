# External scheduler setup (beginner guide)

This is a one-time setup of about 30 minutes. You do every step yourself in each service's own website.
Never paste a token or key into a chat, an email, a GitHub issue, a commit or this document.

Do this only **after** the scheduling PR is merged into `main`. The new Settlement and Collection monitor
workflows can only be started once they exist on `main`.

What you will set up:

1. A GitHub token that lets cron-job.org start workflows in this one repository.
2. A cron-job.org account with 9 jobs (the primary clock).
3. A healthchecks.io account with 9 checks (alerts that do not depend on GitHub).
4. One GitHub repository secret, `HC_PING_KEY`.
5. A no-cost test.

---

## Part 1. Create the GitHub token

1. Sign in to GitHub as `johnlettieri1`.
2. Click your profile picture (top right), then **Settings**.
3. In the left menu, scroll to the bottom and click **Developer settings**.
4. Click **Personal access tokens**, then **Fine-grained tokens**, then **Generate new token**.
5. Fill in:
   - **Token name:** `cron-job-org-nfl-dispatch`
   - **Description:** `Starts NFL collection workflows from cron-job.org`
   - **Resource owner:** `johnlettieri1`
   - **Expiration:** 90 days. Put a reminder in your calendar one week before it expires.
   - **Repository access:** **Only select repositories**, then pick `johnlettieri1/nfl-quant-edge-engine`.
     Nothing else.
6. Under **Permissions**, open **Repository permissions** and set **Actions** to **Read and write**.
   GitHub adds **Metadata: Read-only** by itself; that is required and fine. Leave every other permission at
   **No access**. Do not add any account permissions.
7. Click **Generate token**. GitHub shows the token once. Keep this browser tab open; you will paste it into
   cron-job.org in Part 2 and nowhere else.

What this token can and cannot do (please read):

- It is **not** a dispatch-only token. GitHub has no permission that narrow. "Actions: Read and write" on this
  repository also lets the holder start any workflow with any inputs (including a paid manual snapshot), re-run
  or cancel runs, delete run logs, artifacts and caches, and turn workflows on or off.
- It cannot read secret values, change code, change repository settings or reach any other repository.
- If it ever leaked, paid misuse is limited by the safety guards: at most 30 pulls per NFL week, no pull below
  60 remaining credits, and HALT on any pull that costs more than 1 credit. Revoke it right away (Part 7).
- After the PR is merged, delete old feature branches you no longer need. A workflow can be started on any
  existing branch, so fewer branches means less old code that could be started.

## Part 2. Create the cron-job.org jobs (primary clock)

1. Go to https://cron-job.org and create a free account. Turn on two-factor sign-in in your account
   settings if it is offered.
2. In account settings, set your time zone to **America/New_York**.
3. For each row in the table below, click **Create cronjob** and fill in:

   **Common tab**
   - **Title:** from the table
   - **URL:** `https://api.github.com/repos/johnlettieri1/nfl-quant-edge-engine/actions/workflows/<WORKFLOW FILE>/dispatches`
     (replace `<WORKFLOW FILE>` with the file from the table). For example, jobs 1 to 3 use
     `https://api.github.com/repos/johnlettieri1/nfl-quant-edge-engine/actions/workflows/weekly.yml/dispatches`
   - **Execution schedule:** choose **Custom** and enter the crontab expression from the table
     (minute, hour, day of month, month, day of week). If the page only offers checkboxes, tick the same
     minutes, hours and weekdays.

   **Advanced tab**
   - **Time zone:** America/New_York
   - **Request method:** POST
   - **Headers** (add three):

     | Key | Value |
     |---|---|
     | `Authorization` | `Bearer ` followed by your token (type the word Bearer, one space, then paste) |
     | `Accept` | `application/vnd.github+json` |
     | `X-GitHub-Api-Version` | `2022-11-28` |

   - **Request body:** the JSON from the table, exactly as shown
   - **Timeout:** 30 seconds

   **Notifications tab**
   - Turn on: notify when execution fails; notify when it succeeds again after failing; notify when the job is
     disabled after too many failures.

   Save. Together these send the header `Authorization: Bearer <your token>` over HTTPS. The token travels
   only in that header. Never put it in the URL.

| # | Title | Workflow file | Schedule (ET, crontab) | Plain English | Request body |
|---|---|---|---|---|---|
| 1 | NFL weekly Tue | `weekly.yml` | `11 10 * * 2` | Tue 10:11 AM | `{"ref":"main","inputs":{"kind":"auto","source":"external"}}` |
| 2 | NFL weekly Thu/Sat/Sun | `weekly.yml` | `11 9 * * 0,4,6` | Thu, Sat, Sun 9:11 AM | `{"ref":"main","inputs":{"kind":"auto","source":"external"}}` |
| 3 | NFL weekly Sun night | `weekly.yml` | `31 23 * * 0` | Sun 11:31 PM | `{"ref":"main","inputs":{"kind":"auto","source":"external"}}` |
| 4 | NFL pregame Sun | `pregame.yml` | `*/5 9-20 * * 0` | Sun every 5 min, 9:00 AM to 8:55 PM | `{"ref":"main","inputs":{"source":"external"}}` |
| 5 | NFL pregame Mon | `pregame.yml` | `*/5 18-20 * * 1` | Mon every 5 min, 6:00 to 8:55 PM | `{"ref":"main","inputs":{"source":"external"}}` |
| 6 | NFL pregame Thu | `pregame.yml` | `*/5 12-20 * * 4` | Thu every 5 min, noon to 8:55 PM (covers Thanksgiving) | `{"ref":"main","inputs":{"source":"external"}}` |
| 7 | NFL pregame holidays | `pregame.yml` | `*/5 12-20 * 11,12,1 3,5,6` | Wed, Fri, Sat every 5 min, noon to 8:55 PM, Nov to Jan | `{"ref":"main","inputs":{"source":"external"}}` |
| 8 | NFL settlement | `settle.yml` | `30 10 * * 2,3,4` | Tue, Wed, Thu 10:30 AM | `{"ref":"main","inputs":{"source":"external"}}` |
| 9 | NFL monitor | `monitor.yml` | `0 8,20 * * *` | Every day 8:00 AM and 8:00 PM | `{"ref":"main","inputs":{"source":"external"}}` |

Why these times:

- Weekly jobs fire one minute after each frozen target (Tue 10:10, Thu/Sat/Sun 9:10, Sun 11:30 PM ET), so a
  few seconds of clock difference can never land a run before its target.
- Pregame jobs only start the free gate. The gate pays only when a game is inside its T-30 to T-10 window (or
  the single late try to T-5) and nothing has quoted it yet. Extra runs cost nothing.
- GitHub answers each successful dispatch with HTTP 204 (no content). cron-job.org counts that as success.
  A 401 means the token is wrong or expired; a 404 usually means a typo in the URL or that the PR is not merged.

## Part 3. Create the healthchecks.io checks (alerts outside GitHub)

1. Go to https://healthchecks.io and create a free account (email alerts are included).
2. Create a project named `NFL collection`.
3. Open **Settings** for the project. Under **API Access** (ping key), click to create a **ping key**. Copy it;
   you will put it into GitHub in Part 4 and nowhere else.
4. For each row below, click **Add Check**, then:
   - set **Name** and **Slug** to the slug in the table
   - click **Change Schedule**, choose **Cron**, enter the expression, set **Time zone** to
     America/New_York and the **Grace time** from the table (for the last row choose **Simple** instead)

| Slug | Schedule | Grace time | What a missing ping means |
|---|---|---|---|
| `nfl-tue-scheduled` | Cron `10 10 * * 2` | 1 hour | Tuesday snapshot not captured yet |
| `nfl-thu-scheduled` | Cron `10 9 * * 4` | 1 hour | Thursday snapshot not captured yet |
| `nfl-sat-scheduled` | Cron `10 9 * * 6` | 1 hour | Saturday snapshot not captured yet |
| `nfl-sun-scheduled` | Cron `10 9 * * 0` | 1 hour | Sunday morning snapshot not captured yet |
| `nfl-sun-night-scheduled` | Cron `30 23 * * 0` | 1 hour | Sunday night snapshot not captured yet |
| `nfl-settle` | Cron `30 10 * * 2,3,4` | 2 hours | Settlement did not run |
| `nfl-monitor` | Cron `0 8,20 * * *` | 2 hours | Monitor did not run, OR it found a problem |
| `nfl-pregame` | Simple: period 8 days | 1 day | No successful pregame pull in 8 days |

5. Under **Integrations**, keep **Email** on (it is on by default). You can also add Microsoft Teams or the
   mobile app. Use **Send test notification** to confirm you receive it.

How to read alerts: healthchecks.io emails you when a check goes **down** and again when it comes back **up**.
It does not repeat emails while the same problem continues. A "down" weekly check one hour after its target
means the external run did not complete; the GitHub cron backup may still capture it later (labeled delayed),
and the check will then go back up.

A green weekly, pregame or settlement check always means the work was read back from the records branch on
GitHub, not just that a workflow finished. If a check turns red right after a run, open that run in GitHub
Actions: a paid capture that could not be saved to records is kept as a downloadable artifact named
`unverified-capture-...` for 30 days. You will also see small `claim_...json` files appear under
`data/odds/claims/` on the records branch; they are normal (each marks one paid pull about to happen).

## Part 4. Add the heartbeat secret to GitHub

1. Open the repository on GitHub, then **Settings**, then **Secrets and variables**, then **Actions**.
2. Click **New repository secret**.
3. **Name:** `HC_PING_KEY`. **Secret:** the healthchecks.io ping key from Part 3. Click **Add secret**.

GitHub hides secret values in logs. Until this secret exists the heartbeat steps simply do nothing.

## Part 5. No-cost end-to-end test

None of these steps spends an Odds API credit.

1. **Monitor:** in cron-job.org, open job 9 (NFL monitor) and use **Test run** (or "Execute now").
   - cron-job.org shows status 204.
   - In GitHub, open **Actions**: a new **Collection monitor** run appears with event `workflow_dispatch`.
   - When it finishes, healthchecks.io shows a ping on `nfl-monitor` (green if no findings, red if it found
     something; any finding also appears under **Issues** with the label `collection-alert`).
2. **Settlement:** test run job 8. A **Settlement** run appears. Its log ends with an `appended` count (0 is
   normal when nothing new is final). `nfl-settle` gets a ping. No Odds API key is used by this workflow.
3. **Pregame gate:** test run job 4 at a time with no game in the next 30 minutes (for example a weekday
   morning). The **Pregame planner** run shows the `gate` job saying `pull=False`, and the `pull` job is
   skipped. The gate uses only the free events list.
4. **Weekly gate (optional, only in a safe time):** test run job 1 only during one of these periods, when no
   scheduled window is open: Wednesday 5:00 AM to Thursday 9:00 AM, Friday 4:00 AM to Saturday 9:00 AM, or
   Monday 6:00 PM to Tuesday 10:00 AM (all ET). The log must say `kind=none` and `no live scheduled window`.
   Outside those periods a test run could take a real 1-credit snapshot (this is safe and capped, but it is
   not a no-cost test).
5. **Header check:** in each cron-job.org job's execution history the status should be 204. If you see 401,
   re-enter the `Authorization` header (it must start with `Bearer ` and a space).

## Part 6. What to watch in the first live week

- Thursday 9:11 AM: a Weekly totals run with event `workflow_dispatch`, and `nfl-thu-scheduled` green.
- Thursday: Pregame planner runs every 5 minutes from noon (almost all do nothing), and one `pull` job runs
  between about 7:45 and 7:55 PM ET for the 8:15 PM game.
- The records commit for each capture says `trigger: external_dispatch` and `delayed: false` in the raw file.

## Part 7. Revoke or rotate the token

**Rotate (every 90 days, or sooner):**

1. GitHub, **Settings**, **Developer settings**, **Personal access tokens**, **Fine-grained tokens**, click
   `cron-job-org-nfl-dispatch`.
2. Click **Regenerate token**, choose 90 days, confirm. Copy the new value.
3. In cron-job.org, open each of the 9 jobs, replace the `Authorization` header value with `Bearer ` plus the
   new token, and save.
4. Test run job 9 (monitor) and confirm status 204.

**Revoke (if you think it leaked):**

1. Same GitHub page, click **Delete** on the token. It stops working immediately.
2. cron-job.org jobs will fail with 401 and email you. The GitHub cron backup keeps collecting (possibly late).
3. Create a new token (Part 1) and update the 9 jobs when ready.

**Heartbeat key:** in healthchecks.io project settings you can revoke and recreate the ping key. Update the
`HC_PING_KEY` GitHub secret with the new one.

## Part 8. Suspend automated collection

| Goal | How |
|---|---|
| Pause only the external clock | cron-job.org: switch each job off (toggle on the job list). GitHub cron backup keeps running. |
| Stop all paid collection | GitHub, **Actions**, select **Weekly totals**, click the **...** menu, **Disable workflow**. Do the same for **Pregame planner**. Settlement and monitor can stay on; they never spend credits. |
| Stop everything | Disable all four workflows in GitHub Actions and switch off all cron-job.org jobs. In healthchecks.io, **Pause** each check so it does not email you. |
| Resume | Re-enable the workflows, switch the jobs back on, un-pause the checks. |

A HALT file on the records branch (created automatically if a pull ever costs more than 1 credit) also stops
every paid pull until you approve removing it. Settlement and monitoring continue during a HALT.
