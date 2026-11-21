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
`report-DATE.md` and `report-DATE.json` are the alias for the newest build, and the backtest files hold the newest build too.
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
`daily-report.sh --skip-unchanged` downloads the newest versioned asset `report-DATE-HHMMSSZ.json` of the release after it predicts, and `hockey-model report-compare` compares the two hashes.
The versioned asset is the reference, because the publish script uploads the alias `report-DATE.json` after the versions, and a failed alias upload leaves the alias behind the newest build.
A release with no versioned asset falls back to the alias.
Only the release calls of the compare step read the token, as `REPORT_GH_TOKEN`.
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
The cause of that delay is still open in #280.
The operator can run the `daily-prepare` workflow with a date, then run the report again.
A prepare that fails every hour shows as red `daily-prepare` runs. The green report skips before the first puck drop do not show it.

Both workflows share one concurrency group, and GitHub keeps one waiting run for a group.
A newer waiting run replaces an older one, so a manual `force` prepare that waits behind a running prepare could be replaced by the next run that joins the group (#285).
The `prepare` job of `daily-prepare.yml` holds the group, and a `gate` job before it has none.
On a scheduled run the gate calls `scripts/manual-prepare-pending.sh`, and the run stands aside while a manual `daily-prepare` run is pending, queued, waiting, or running.
A manual dispatch never stands aside.
A failed lookup fails the gate, so the hourly run turns red and the next hour retries.
Two cases remain.
The `build` job of `daily-report.yml` can still take the waiting slot of a manual prepare, because the report must not wait for an operator's rebuild.
A scheduled gate and a manual dispatch that start in the same few seconds can also race.

Both workflows share one concurrency group, because both write the cached store.
The group is on the `build` job of `daily-report.yml`, not on the workflow, so the gate job starts at the trigger.
A workflow-level group holds the gate back until the prepare ends, and the gate then reads an empty group (#163).
GitHub counts a job timeout from the job start, so a full prepare can hold the group for `PREP_TIMEOUT` (the prepare job timeout, 180 minutes) past its start.
The gate reads the start of the prepare run that holds the group, and it skips a due run that would wait, with reason `prepare-wait`.
The wait ends when the prepare ends, so the report's own budget starts then and adds to the prepare's timeout.
A forced run still queues, because the operator asked for it.
The concurrency group allows one running run and at most one waiting run.
A newer waiting run replaces the older one, so a replaced run never publishes.
Whichever run survives checks the release before it builds, so correctness does not depend on the queue.
A prepare that skips a prepared day holds the group for seconds, so an hourly prepare does not block the report.
Only the one prepare of a new day holds the group for hours.

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
