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

## Trigger hours

A trigger fires at minute `TRIGGER_MINUTE_UTC` of each hour in `TRIGGER_HOURS_UTC` in `src/hockey_model/report_trigger.py`, and the workflow cron lists the same minute and hours.
The trigger minute is not :00.
GitHub delays and drops scheduled runs most at the top of the hour, and many workflows also use :15, :30, and :45.

`scripts/trigger_hours.py` reads the season schedule and prints the hours that the first puck drops need.
For each game day it prints the trigger derived from the last top-of-hour slot before the first puck drop. That derivation predates the first-run rule, and its update is tracked in #162.
The script MUST be run again each season, and again when the playoff schedule comes out.
A game day with no listed trigger before its puck drop gets no report before the puck drop, so the script must list an hour for every game day.

## Which run publishes

Every trigger that starts while the first puck drop of the game day is still ahead is due.
The gate compares the time of the run, plus the report budget, with the first puck drop. A run that cannot finish before the puck drop does not publish.
The first due run that starts publishes the report.
A later run before the puck drop builds the report again and adds its version to the release, so the report can change until the first puck drop.
Each build keeps its own files, named `report-DATE-HHMMSSZ`, where the time is its `generated_at`, and nothing published is replaced (#196).
`report-DATE.md` and `report-DATE.json` are the alias for the newest build, and the backtest files hold the newest build too.
The notes say so, and they name the commit and time of each build.
After the puck drop, every run skips a day that has a release.
The release target stays the commit of the first build, so the commit of a replacing build appears only in the notes and in the JSON copy.
A game day with no games publishes nothing, so a failed schedule read cannot leave a wrong release.

This replaces the earlier rule, which published only at the last trigger before the puck drop.
That rule missed the puck drop whenever the last run started late (#159).
The new rule trades freshness for coverage: an early run publishes with the starters known at that time.
The goalie starters are announced close to the puck drop, so a report published several hours early may use starters that change later.
The owner has to confirm this trade-off before the rule ships.

## Two runs per game day

The slow work does not fit between a trigger and the puck drop.
The fit and the backtest each take minutes or more on a full store.

1. `daily-prepare.yml` runs once each morning, before the first trigger hour. It refreshes every source, fits the model, and runs the backtest. Its job timeout fits the window from its cron hour to the next listed trigger hour (#149).
2. `daily-report.yml` runs at each trigger. When a trigger is due, it refreshes only the schedule, the goalie sources, and the injuries, then predicts and publishes.

When the morning run did not finish, the report run fails and says so.
A report run more than `PREP_WINDOW` (180 minutes, the prep job timeout) before the first puck drop skips with a notice and exits 0, because the morning run has not had its window yet (#223).
The same missing marker inside that window still fails, because a person has to act.
It does not do the morning work itself, because that work is too long to fit before the puck drop and the report run must finish in minutes.
The operator runs the `daily-prepare` workflow with that date, then runs the report again.
Both workflows share one concurrency group, because both write the cached store.
The concurrency group allows one running run and at most one waiting run.
A newer waiting run replaces the older one, so a replaced run never publishes.
Whichever run survives checks the release before it builds, so correctness does not depend on the queue.

## Job overheads

Each job logs its wall time as a notice: the gate, the build, and the publish job.
The overheads are measured from these notices before the trigger lead is set.

## Missed days

A run after the puck drop with no release writes a warning and publishes the first report of the day late.
The games that have not started still need the report, so a late first report is better than none.
A late report predicts the games that are finished as known at their puck drop, so it is full.
A separate scheduled check alerts when a game day has no release, or when its only report has every row made after the start (#112).

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
The clock's cron minute and hours MUST match `TRIGGER_MINUTE_UTC` and `TRIGGER_HOURS_UTC`.
