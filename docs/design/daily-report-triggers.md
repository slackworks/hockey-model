# Daily report triggers

## Problem

The daily report is published before the first puck drop of each game day. When no run did, the first report of the day is published late.
A report published after puck drop is useless for scoring, because a reader drops every prediction made after the start.
GitHub Actions schedules run in UTC, and a scheduled run can start 10 to 60 minutes late, or be dropped.
A fix that depends on one run starting on time fails when that run is late.

## Game day

A game day is the Eastern date that the NHL schedule and score feed use.
A 7 pm ET slate starts after midnight UTC for much of the season, so a UTC date splits one night's games across two reports.
Every day-level key uses the Eastern game day: the report date, the release tag, the store read, and the gate.
Every game start stays a UTC instant.
`src/hockey_model/game_day.py` holds the time zone and the day window.

## Hourly schedule

Both workflows run every hour (#282).
The report fires at minute `TRIGGER_MINUTE_UTC` of each hour in `src/hockey_model/report_trigger.py`, and the cron in `daily-report.yml` lists the same minute.
The prepare fires at minute 14, half an hour away from the report, so each job runs in the middle of the gap between two runs of the other.
Neither minute is :00.
GitHub delays and drops scheduled runs most at the top of the hour, and many workflows also use :15, :30, and :45.

An hourly cron covers every first puck drop, so no list of trigger hours and no yearly re-derivation exist (#162).
A late or failed run is retried at the next hour.

## Which run publishes

Every trigger that starts while the first puck drop of the game day is still ahead is due.
The gate compares the time of the run, plus the report budget, with the first puck drop. A run that cannot finish before the puck drop does not publish.
The first due run that starts publishes the report.
A later run before the puck drop builds the report again, and it publishes only when its inputs differ from the newest published report (#282).
Each build keeps its own files, named `report-DATE-HHMMSSZ`, where the time is its `generated_at`, and nothing published is replaced (#196).
`report-DATE.md` and `report-DATE.json` are the alias for the newest build, the build with the latest `generated_at`, and the backtest files hold the newest build too.
A retry of an older build adds its own files and its index entry, and leaves the alias, the backtest files, and the notes with the newest build (#318).
The notes say so, and they name the commit and time of each build.
After the puck drop, every run skips a day that has a release.
The release target stays the commit of the first build, so the commit of a replacing build appears only in the notes and in the JSON copy.
A game day with no games publishes nothing, so a failed schedule read cannot leave a wrong release.
The gate reads `games_outside_window` as well as `games`.
A day with no game inside its window and a non-zero outside count fails, because the feed probably lists the day under the wrong date.
A day with some games inside and some outside warns and still decides, because a dropped row publishes a partial day.

The report JSON holds `inputs_sha256`, the hash of what a reader sees.
It covers the date and the interval level, the model identity (features, `fit_sha256`, `code_sha`), `training_gaps`, and for each game the ID, the start, the state, the skip note, both win chances, `made_after_start`, `missing`, `degraded_inputs`, and `training`.
It also covers the source `credits`.
It leaves out `generated_at` and the age in a stale-source note, because time passing is not an input.
The report JSON shows the goalie and injury state through the win chances and `missing`, not as raw reads.
A hash of those values changes exactly when a read changes what the report says, and a fresh read of the same value changes nothing.
`daily-report.sh --skip-unchanged` reads the release of the day after it predicts.
It compares only when the release is complete for its newest build: `index-DATE.json` names the newest build, `report-DATE.json` has the `generated_at` of that build, and the notes name it.
The publish script uploads the versions, then the alias and the index, then the notes, so a failed step leaves the release behind its newest build (#318).
A release that is behind, which includes a release from before the index, builds and publishes again, and that publish repairs it.
A complete release goes to `hockey-model report-compare`, which compares the hash of the build with the hash in `report-DATE.json`.
Only the release calls of the compare step read the token, as `REPORT_GH_TOKEN`. The script unsets that variable once at the top and re-exports it as `GH_TOKEN` inside the compare step, so no other command carries a token.
The gate reads its own token the same way: `report-gate.sh` unsets `GH_TOKEN` at the top and re-exports it on the release read alone, so the feed read and the decision call carry none.
Equal hashes end the run with reason `unchanged` and no `complete` marker, so the publish job skips.
An alias with no `inputs_sha256`, from a build before the field existed, counts as changed once.
The hash covers `schema_version`, so a build with a new report shape publishes again. The shape and the build index are in [report-contract.md](report-contract.md).
A failed release lookup or download stops the run with exit code 1.
The workflow passes the flag only when the gate decided to publish, so a dry run always builds.
A day with no release builds and publishes without a comparison, which keeps the first report of a day and the late report.

The rule trades freshness for coverage: an early run publishes with the starters known at that time, and a later run publishes again when a starter is confirmed.

## Two runs per game day

The slow work does not fit between a trigger and the puck drop.
The fit and the backtest each take minutes or more on a full store.

1. `daily-prepare.yml` runs every hour. It refreshes every source, fits the model, and runs the backtest, but only for a day that is not yet prepared.
2. `daily-report.yml` runs every hour. When a run is due, it refreshes only the schedule, the goalie sources, and the injuries, then predicts and publishes when the inputs changed.

A prepare for a day that is already prepared skips the backtest and the fit (#281).
The day is complete when `prepared-DATE` exists and `backtest-DATE.json` holds a `live_features` list of names.
The refresh still runs, because it is cheap and keeps the store current for the report.
A marker without a valid backtest file rebuilds, and says why on stderr.
`--force` rebuilds on purpose. Only a manual dispatch passes it, so a scheduled run never forces.
The stricter rule, a check that the backtest inputs did not change, costs a store query and buys nothing today, because the report refreshes the starters itself and the backtest scores past games.

A report run that finds no prepared marker retries at the next hour.
Before the first puck drop of the day, the run skips with a notice and exits 0, because the next prepare can still make the marker.
After the first puck drop, the same missing marker fails, because a person has to act.
The report no longer assumes that one morning prepare finishes inside a fixed window.
`PREP_WINDOW`, `PREP_QUEUE_ALLOWANCE`, `PREP_CRON_HOUR_UTC`, `prep_too_early`, and the `prep_too_early` field of `report-due` are gone (#280).
The measured start delay of 6.7 to 10.4 hours for the old `0 8 * * *` cron no longer matters, because the next hourly run replaces a late one.
The delay sits in GitHub's scheduler (#280).
For each of the 16 scheduled runs of the 30 days before 2026-11-21, the `created_at` time of the run, its `run_started_at` time, and the `started_at` time of its job were the same, within a minute.
The run was therefore created late, and it did not wait in the concurrency group or in a runner queue.
The default branch held the cron `0 8 * * *`, and no scheduled run was cancelled near 08:00 UTC.
The report cron of the same repository runs with a much shorter delay, so the repository is not blocked from scheduled runs.
A repository or organization setting is not ruled out, because the token of the check cannot read the Actions permissions of the repository.
GitHub documents that scheduled runs at the top of the hour are the most delayed.
The hourly prepare runs at minute 14 for that reason, so its delay is the first measurement that can confirm the cause.
The operator can run the `daily-prepare` workflow with a date, then run the report again.
A prepare that fails every hour shows as red `daily-prepare` runs. The green report skips before the first puck drop do not show it.

Both workflows share one concurrency group, and GitHub keeps one waiting run for a group.
A newer waiting run replaces an older one, so a manual `force` prepare that waits behind a running prepare could be replaced by the next run that joins the group (#285).
The `skip` and `prepare` jobs of `daily-prepare.yml` hold the group, and a `gate` job before them has none.
On a scheduled run the gate calls `scripts/manual-prepare-pending.sh`, and the run stands aside while a manual `daily-prepare` run is pending, queued, waiting, or running.
A manual dispatch never stands aside.
A failed lookup fails the gate, so the hourly run turns red and the next hour retries.
A report run that would only replace a published day stands aside in the same way (#295).
Its gate calls the same script before it decides, and it skips with reason `manual-wait` while a manual `daily-prepare` run is pending, queued, waiting, or running.
The report keeps the slot only when the day has no release, because a day with no report is worse than a delayed rebuild.
A report hour that stands aside leaves the published report in place, and the next hour tries again.
A failed lookup fails the gate, for the same reason as on the prepare side.
One case remains.
A scheduled gate and a manual dispatch that start in the same few seconds can race.

Both workflows share one concurrency group, because both write the cached store.
The group is on the `build` job of `daily-report.yml`, not on the workflow, so the gate job starts at the trigger.
A workflow-level group holds the gate back until the prepare ends, and the gate then reads an empty group (#163).
GitHub counts a job timeout from the job start, so a job can hold the group for its own timeout past its start.
The prepare is two jobs, because a prepared day and a rebuild hold the group for very different times (#311).
The `skip` job restores the cache, runs `daily-prepare.sh --check-only`, and saves the cache, with a 10-minute timeout.
Exit code 4 means the day needs the rebuild, and the `prepare` job then runs with the 180-minute timeout.
A prepared day ends in the `skip` job, so it holds the group for minutes.
The group frees only after every holder ends, so the holder that ends last is the one the report waits for.
`scripts/prep-running.sh` reads the job list of each live `daily-prepare` run and prints the end of the window of that holder.
The gate passes the end to `report-due` as `--prep-ends`, and a due run whose first puck drop falls at or before that end skips with reason `prepare-wait`.
The wait ends when that job ends, so the report's own budget starts then and adds to the job window.
A forced run still queues, because the operator asked for it.
The concurrency group allows one running run and at most one waiting run.
A newer waiting run replaces the older one, so a replaced run never publishes.
Whichever run survives checks the release before it builds, so correctness does not depend on the queue.
Only the `prepare` job of a day that needs the rebuild holds the group for hours.
All three commands name the job timeouts, so `daily-report.yml` passes `--timeout skip=10` and `--timeout prepare=180`.
A job name that the workflow does not carry matches nothing, and the answer then reads as no prepare.
A test compares those flags with the `timeout-minutes` of each job, so a rename that misses the workflow fails the gate.

The `prepare` job of the split prepare does not exist in the job list while `skip` runs, and it never exists when the day needs no rebuild.
The lookup therefore refuses no job name at run time, and an hour that finds a name matches nothing.
A run that has started no holder yet names the longest timeout instead, because the group can admit the rebuild job first.
A forced dispatch skips the `skip` job and goes straight to the rebuild, so the long window is the one to plan for.

A rebuild day is the remaining case.
The `skip` job at minute 14 can find that the day needs the rebuild, and then the `prepare` job takes the group for up to 180 minutes.
The report gate at minute 44 reads only the window of `skip`, because `prepare` does not exist in the job list while `skip` runs.
A `skip` job ends in seconds on a warm cache, so its window never stops the report, and the gate can admit a report build for a day that needs the rebuild.
The race runs in the gap between the end of `skip` and the start of `prepare`, while `build` is the only run that waits.
A `build` that runs when `prepare` arrives keeps running, and `prepare` waits behind it.
A `build` that still waits when `prepare` arrives is cancelled, because the newer waiting run replaces it.
A cancelled build changes no release: a day with a release keeps it, and a day with none is built by the next hour.
The split from #311 accepts that race to keep the hold of a prepared day to minutes.


## Job overheads

Each job logs its wall time as a notice: the gate, the build, and the publish job.
The overheads are measured from these notices before the trigger lead is set.

## Missed days

A run after the puck drop with no release writes a warning and publishes the first report of the day late.
The games that have not started still need the report, so a late first report is better than none.
A late report predicts the games that are finished as known at their puck drop, so it is full.
A separate scheduled check alerts when a game day has no release, or when its only report has every row made after the start (#112).

## Live feeds that stay down

A refresh reads a live feed on every run and records no error when the feed is already a known gap (#276).
So a feed that stays down does not fail the prepare, and the report publishes with the feed in `missing`.
The same scheduled check turns a gap that is older than `LIVE_FEED_MAX_AGE` into a red job (#278).
`hockey-model store gaps --live` lists each gap of the three live feeds as JSON, with the time of its first failure, its age, and `over_limit`.
`scripts/check-live-feeds.sh` prints one `::error::` line for each gap that is over the limit, and the line names the source, the URL, and the age.
A gap of a dated source does not raise this alert, because a newer score day or MoneyPuck file covers it.
A store that no run writes any more makes no new failure, so no gap ages.
The same command also lists the newest read of each live feed, and the check fails for a feed whose newest read is older than `LIVE_FEED_MAX_AGE`, or that has none (#294).
A good read clears the gap with nobody involved, so the alert needs no operator step except a check of the host.

A feed that returns a permanent HTTP status, such as 403, becomes a gap on the second failed read, with no wait for `ABANDON_MIN_SPAN_DAYS` (#279).
The prepare of that day then skips the feed at once, so the slate is not lost.
To recover, check why the host refuses the runner.
The next good read clears the gap by itself.

## Actions cost

The repository is public, and GitHub-hosted runners on public repositories have no minute charge.
A trigger that is not due runs one short gate job.
The numbers for one month are in the pull request that added this design.

## External clock

GitHub's `schedule` trigger ran the 23:44 UTC slot about 50 to 60 minutes late on two days in eight (#216, #222).
The late run then fell after the puck drops, so most of the day's games were made after their start.
The design is an external clock that calls `workflow_dispatch` on `daily-report.yml` at each trigger minute, so the slot does not depend on GitHub's queue.
The `schedule` trigger stays as a fallback.
The gate already skips a duplicate trigger once the release exists, so a dispatch and a late schedule run do no harm.
The owner provisions the clock. Use a fine-grained token limited to this repository with only the Actions write permission, plus Metadata read, with a set expiry, stored in the clock host's secret store.
Until the owner provisions the clock, only the `schedule` trigger runs.
The clock runs `gh workflow run daily-report.yml --repo slackworks/hockey-model` at each slot. The dispatch omits `--ref`, so it always runs the default branch.
The clock's cron minute MUST match `TRIGGER_MINUTE_UTC`.
