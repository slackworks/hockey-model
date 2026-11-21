# Progress logging

## Problem

A long run printed nothing until it ended, so a reader of the job log could not tell a working job from a hung one (#133).
A daily-prepare run printed the refresh report once, and then the log stayed silent for many minutes.

## Where the silences were

The first step of #133 is to find the silent gaps.
The run of the backtest for #143 on the real store shows them.
Its stderr held one line for each finished unit, and the gap between two lines was up to 340 seconds, because the season builds are units of several minutes.
The refresh printed its JSON report once at the end of the run, and the backfill windows printed nothing.
The fit printed nothing at all.
The shell scripts printed one line for each step, and only after the step ended.

## Choice

One module, `src/hockey_model/progress.py`, writes every progress line with the standard `logging` module, on the logger `hockey_model.progress`.
A line has one format:

```
progress COMMAND STEP EVENT elapsed=12.3s done=3/10 budget_left=60s reason=TEXT
```

The events are `start`, `tick`, `stop`, and `end`.
A `tick` comes from a heartbeat timer at a set interval, so a step with no natural count still shows that it works.
A step that raised, or that named a reason with `progress.stopped`, ends with `stop` and the reason.
A step with a known count shows `done=N/TOTAL`, and a step with a time budget shows what is left.

Library code only calls the logger, so a test or the consumer API stays quiet.
`cli.py` installs a stderr handler for the store commands, so the JSON on stdout does not change.

## Where the steps are

- `store_cli.run_store_command` runs each long command in a step named `run`. The heartbeat of that step covers the fit, the backtest, and every command with no count of its own.
- The refresh runs the score days and the boxscores of each backfill window in a step named with the date range. The steps show the count read and left, the budget left, and the reason for a stop.
- `run_units` of the backtest runs in a step with the count of units.
- `scripts/run-step.sh` writes the same lines for the shell scripts. `daily-prepare.sh` wraps the upgrade, the refresh, the backtest, and the fit, and `daily-report.sh` wraps its three steps.

## Edge cases

- The budget left of a refresh comes from the time of the last read, so the progress line never reads the clock of the refresh again.
- A step name is one word, so a line splits on spaces. A date range in the name uses two dots.
- The heartbeat thread is a daemon, and it stops in a `finally`, also when the step raises.
