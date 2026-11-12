# Daily prepare state machine: TLA+ model

Tracks issue #169.

This note is the full build plan.
An implementer copies the reference spec in the appendix, adds the files in "Files to add or change", and runs the checks in "Verification".
TLC already checked the reference spec and every break test in this note.

## Problem

`scripts/daily-prepare.sh` keeps state across runs.
While the marker `$HOCKEY_MODEL_DB.backfilled` is missing, each run calls `hockey-model refresh --backfill-from ... --budget-seconds "$BACKFILL_BUDGET_S"`.
The backfill reads the store one window at a time, newest window first, until the time budget is spent.
A run that does not finish writes no marker, and the next run resumes, because refresh skips what it already read.
After the marker exists, each run does a plain refresh, the backtest, and the fit, and then writes `prepared-DATE`.

The tests check single runs.
No test checks the rules that must hold across runs.
The risk is in two places:

- Safety: the marker appears before every window is read.
- Liveness: the backfill never finishes, so the marker never appears and the daily report never runs.

## Options

1. Model the backfill windows, the budget stop, the abandon rule, the exit code, and the marker in TLA+. Check safety and liveness with TLC in CI.
2. Also model the store cache and the plain refresh after the marker.
3. Add more property tests only.

## Choice

Option 1.
The cross-run rules are where example tests miss a bug.
Hypothesis tests (option 3) check one call, and they cannot state "eventually, over many runs".
Option 2 needs the branch-scoped GitHub cache and the plain refresh path, which doubles the model for little gain.

## What the code does

The spec mirrors these rules.
Each rule names the code that owns it.
The prose names constants and does not repeat their values, because the values live in the code.

### The window loop: `store/refresh_backfill.py`, `nhl()`

1. `score_gaps()` and `plan_unresolved()` run once, before any read of the run.
   So a URL that becomes abandoned during a run is a gap only from the next run.
2. `windows(first, last)` yields windows of `WINDOW_DAYS` days, newest window first.
   `last` is the game day of `now` plus `SCHEDULE_DAYS_AHEAD`, so the newest window holds days that are not closed yet.
3. In each window, `read_days()` runs first, and then `read_boxscores()` runs.
   `read_boxscores()` runs even when `read_days()` stopped.
   The loop breaks after `read_boxscores()` returns, when either call stopped.
4. `complete` is the AND of `clean` over every window that the loop visited.
   A stopped call is never clean, so a break always leaves `complete` false.
5. `read_days()` and `read_boxscores()` count the units that they finished for good and that are older than `BACKLOG_BEFORE` before the game day of `now`.
   A score day is finished when its read made it complete.
   A boxscore is finished when its read worked.
   An open day and a due day do not count, because the next run reads them again.
   A day that closed since the last run does not count, because a daily run completes one such day first.
   Without this rule, that day would count as progress, and a run that the budget stops would exit 75 and never fail.
6. After the loop, `nhl()` adds an error when the loop stopped, `run.out_of_time()` is true, and no unit was finished.
   The error names `--budget-seconds`, so the refresh exits 1 and not 75 (#178).
   A due day whose re-read resolves its game counts as zero, so a run that spends its budget on due days gets the error once.
   The next run continues, so this is a single false exit 1 and not a loop.

### Score days: `store/refresh_nhl.py`, `read_days()`

The days go oldest first in the window.
For each day, in this order:

1. If `failures >= MAX_UNIT_FAILURES`, stop.
   The check runs at the top of the loop, so it fires only when one more day is left in the list.
   A run of failures at the end of the list is unclean, not stopped.
2. If the day is a gap, or it is complete (`_day_complete()`) and not due, set `failures = 0` and skip it.
   A day is due when it has an unresolved game (`plan_unresolved()`).
   A due day is read again even when it is complete.
3. If `run.out_of_time()`, stop.
4. Otherwise, `run.attempt()` reads the day.
   A failure adds one to `failures`, and a success sets `failures` to zero.

`_day_complete()` is true when the newest good read came `DAY_COMPLETE_AFTER_CLOSE` after the day closed.
A day that is not closed is read in every run, and a good read does not complete it.

### Boxscores: `store/refresh_nhl.py`, `read_boxscores()`

The list is `games_missing_boxscore()`: final games of the window with no boxscore.
A game is known only after a good read of its score day.
Gap games are taken out of the list before the loop, so they do not reset `failures`.
The function notes the number of gap games, not their names.
For each game, in this order:

1. If `failures >= MAX_UNIT_FAILURES`, stop.
2. If `run.out_of_time()`, stop.
3. If the newest read of the game failed less than `RETRY_FAILED_AFTER` ago (`failed_since()`), skip it and set `clean = False`.
   This skip records no error.
4. Otherwise, `run.attempt()` reads the game.

### The abandon rule: `store/source_reads.py`, `abandoned_urls()`

A URL is abandoned when at least `ABANDON_AFTER_FAILED_READS` reads failed since its newest good read, and the first and the last of those failures are at least `ABANDON_MIN_SPAN_DAYS` apart.
An abandoned URL is never read again.
It stays abandoned, even when it had a good read before the failures.

### The exit code: `store_cli.py`, `_refresh()`, and `scripts/daily-prepare.sh`

1. `refresh` exits 1 when any source recorded an error.
   This includes MoneyPuck, ESPN, and Daily Faceoff, which run after the NHL steps (`store/refresh.py`, `_STEPS`).
2. `daily-prepare.sh` exits with that code before it reads `backfill_complete`.
   So a stop after `MAX_UNIT_FAILURES` failures exits 1, not 75.
3. With exit 0 from refresh, the script writes the marker when `backfill_complete` is true, and exits 75 when it is false.
   Exit 75 happens for a budget stop or a boxscore retry skip, with no error from any source.
   A budget stop can also happen in the MoneyPuck step (#136).
   The MoneyPuck step always reads its first stale file, so each run makes progress on those files.
   The TLA+ model does not include this stop.
   A game that a boxscore read leaves still missing (#130) is named in the notes and does not hold the backfill open.

### The workflow: `.github/workflows/daily-prepare.yml`

The `hockey-model-store` concurrency group runs one prepare or report job at a time, so runs never overlap.
The cache save step has `if: always()`, so the store and the marker are saved together after exit 1 and exit 75.

## The model

| Code | Spec |
| --- | --- |
| One score day, one game | One score URL and one box URL for each day. Each day has one game. |
| `WINDOW_DAYS` | `DaysPerWindow` |
| Window count from 2014 to today | `NWindows` |
| `--budget-seconds`, `out_of_time()` | `Budget`: the number of attempts before `out_of_time()` is true. |
| `MAX_UNIT_FAILURES` | `MaxFails`, scaled down so a small window can reach a stop. |
| `ABANDON_AFTER_FAILED_READS` | `AbandonReads` |
| `ABANDON_MIN_SPAN_DAYS` | One tick. `AbandonTicks` is 1. |
| `RETRY_FAILED_AFTER` | `RetryTicks`, in ticks. |
| Days after the newest closed day | `OpenDays` |
| A URL that always fails, such as the score day that always returns HTTP 500 | `BrokenScore`, `BrokenBox` |
| A source after the NHL steps that always fails | `OtherBroken` |
| `source_reads` rows of one URL | `okRead`, `streak`, `span`, `firstAge`, `lastAge` |
| The `.backfilled` file | `marker` |
| A unit that a run finished for good and that the code counts as progress | `progress`, which follows `ArrivalsCount` |
| A unit that a run finished for good and that is not an arrival (ground truth) | `backlog` |
| A day that is not closed yet | `open`, which starts as `OpenDays` and shrinks by one at each tick |
| A day that closed since the last run ended | `arrived` |
| The old rule, where an arrival counts as progress | `ArrivalsCount = TRUE` |
| Read the oldest window first | `BacklogFirst = TRUE` |
| One `daily-prepare.sh` run | The phases `idle`, `nhl`, `other`, `finish` |

Abstractions, and the reason for each:

1. Time passes only between runs, in ticks of `ABANDON_MIN_SPAN_DAYS`.
   One run spends `BACKFILL_BUDGET_S` at most, which is much less than one tick.
   So every read in one run has the same time.
2. `streak` counts failed reads since the newest good read, and it stops at `AbandonReads`.
   `span` records that two failures in the streak are at least one tick apart.
   `firstAge` and `lastAge` count ticks since the first and the newest failure of the streak, and they stop at `AbandonTicks` and `RetryTicks`.
   A good read sets all four to their start values.
   This gives a finite state space with the same answers as the SQL in `abandoned_urls()` and `failed_since()`.
3. The weather: before the `GoCalm` step, any read can fail and any set of read days can be due.
   After `GoCalm`, every read of a URL that is not broken works, and no day is due.
   Fairness makes `GoCalm` happen.
   Without it, a feed that fails at random forever keeps the marker away, which is true of the code and not a bug.
4. Each due set comes from days with a good score read, because a game is known only from a score read.
5. A tick closes the oldest open day.
   `open` holds the days that are not closed, and it starts as `OpenDays`.
   `arrived` holds the days that closed since the last run ended.
   One tick is `ABANDON_MIN_SPAN_DAYS`, which is 0.5 day, so the model closes two days for each real day.
   The model closes at most `|OpenDays|` days, so the state space stays finite.
   No new day arrives, so an endless daily run is not in the model.
   One arrival in one run shows the stall (see the findings).
6. In the code, `BACKLOG_BEFORE` leaves out the days that are too recent to count.
   The model leaves out the days that closed since the last run, which is the case of a daily run.
   The code uses a date cutoff: today minus `BACKLOG_BEFORE`, which is 2 days, or about 4 ticks.
   A second run in the same tick, such as a retry after exit 75, sees `arrived = {}`.
   It can count a day that closed one tick ago, and the code would not count that day.
   So the model counts as much progress as the code, or more, and the code exits 1 in at least as many cases.
   The safety properties are sound in that direction.
   The model cannot show a false exit 1 in the code, where the only work left is within 2 days of today.
   #191 tracks the cutoff as a tick count.
7. `progress` is what the code counts, and it follows `ArrivalsCount`.
   `backlog` is the ground truth: a finished unit that is not an arrival.
   The code chooses the exit code from `progress`.
   The property `BacklogStallExitsOne` uses `backlog`.
8. `BacklogFirst` reads the oldest window first, so the backlog comes before the open days.
   This is the reorder fix from #186.
9. After the marker, the plain refresh reads every missing boxscore (the non-backfill branch of `nhl()`).
   So the model does not require the boxscores of a day that closes after the marker.
   `MarkerOnlyWhenComplete` checks the step that writes the marker, for this reason.
10. A good read of a day that is still open does not set `okRead`.
    The code needs a read after the close (`day_is_complete`, `DAY_COMPLETE_AFTER_CLOSE`), so a day that closes is read again.
    The model pays for that read, which is the read that the old rule counted as progress.
    `streak` and the ages reset on any good read, as in `source_reads`.
    `WindowDone` does not require an open day to be complete.
    A failed or skipped read of an open day leaves the run unclean, so no marker follows.
11. `BacklogStallExitsOne` is the same check as `StallExitsOne` when `ArrivalsCount` is `FALSE`, because `progress` and `backlog` are equal then.
    It exists as the target of the `ArrivalsCount` mutation, and it is not an independent check.

## Constants

`specs/daily_prepare.cfg` uses: `NWindows = 2`, `DaysPerWindow = 2`, `Budget = 2`, `MaxFails = 1`, `AbandonReads = 2`, `AbandonTicks = 1`, `RetryTicks = 2`, `OpenDays = {4}`, `BrokenScore = {1}`, `BrokenBox = {3}`, `OtherBroken = FALSE`, `ArrivalsCount = FALSE`, `BacklogFirst = FALSE`.
Day 1 is the oldest day, so window 1 holds days 3 and 4, and day 4 is open.

`specs/daily_prepare_stall.cfg` is a small model with a budget that cannot cover the open day: `NWindows = 2`, `DaysPerWindow = 1`, `Budget = 1`, `OpenDays = {2}`, `BrokenScore = {}`, `BrokenBox = {}`, and the other constants as above.

With these values, TLC finds 27,181,712 distinct states in the first model, and 295,722 in the stall model.
Before the arrival step, the counts were 4,000,218 and 16,050.
The arrival step, the `arrived` set, and the `backlog` flag multiply the state space.
The first model took 29 minutes 21 seconds with `-workers auto` on the development machine in the #186 run.
Other checks ran on the same machine during that run, so the figure is an upper bound.
The `tla` CI job time for the same spec is the cleaner figure.
The earlier figure, 3 minutes 14 seconds, was on a 10-core Mac, so the two times are not a controlled comparison.
The `tla` CI job ran 30 minutes and was cancelled by its timeout during the liveness check: the search took 29 minutes on the runner.
The job limit is now 60 minutes as a stopgap, and #188 tracks the reduction.
#188 tracks the reduction.
The liveness check of the full graph runs on one thread, so more workers speed up only the search.

`tests/test_tla_constants.py` keeps the values of both `.cfg` files in step with the code:

1. `AbandonReads == ABANDON_AFTER_FAILED_READS`.
2. `AbandonTicks == 1`.
3. `timedelta(days=ABANDON_MIN_SPAN_DAYS) * RetryTicks == RETRY_FAILED_AFTER`. If this is not exact, the test fails and names both constants.
4. `1 <= MaxFails <= MAX_UNIT_FAILURES`.

The test reads the `CONSTANTS` block of the `.cfg` file with one shape regex for each line, `^\s*([A-Za-z]+)\s*=\s*(.+?)\s*$`.
It then checks in code that each needed value is an integer, as `rules/regex-validation.md` says.
A missing or non-integer constant fails the test with a message that names the constant and the file.

### Smaller configs (#185, partial)

Sweep with `ArrivalsCount = FALSE`, all properties on, and 4 workers for each run.

| Config | Distinct states | Time |
| --- | --- | --- |
| `NWindows = 2`, `DaysPerWindow = 1`, `Budget = 1`, `OpenDays = {2}` (the stall `.cfg`) | 295,722 | 12 s |
| `NWindows = 2`, `DaysPerWindow = 1`, `Budget = 2`, `OpenDays = {2}`, `BrokenScore = {1}` | 10,982 | 1 s |
| `NWindows = 3`, `DaysPerWindow = 1`, `Budget = 2`, `OpenDays = {3}`, `BrokenScore = {1}`, `BrokenBox = {2}` | 1,504,428 | 1 min 35 s |
| Full `.cfg` | 27,181,712 | 29 min 21 s (upper bound) |

Every config in the sweep passes.
The full config is not yet replaced in CI.
The reductions, the preservation arguments, the break-test coverage, and the repeated timings are open in #188.

## Properties

Each property names the code rule that it guards, and the break test that proves it is not empty.
The break tests ran against the reference spec, and each one failed as listed.

| Property | Kind | Rule | Break test | TLC result |
| --- | --- | --- | --- | --- |
| `TypeOK` | Invariant | Variable types | None | Passes |
| `MarkerOnlyWhenComplete` | Action property | The step that writes the marker has every score day read or gapped, and every needed boxscore read or gapped. A day still open needs only its score read. | In `Finish`, set `marker' = ~errors`. | Action property violated (stall `.cfg`, #186) |
| `AbandonedIsNoted` | Invariant | Every gap of a run is in the notes. | In `StartRun`, set `noted' = {}`. | Invariant violated |
| `DoneStaysDone` | Action property | A read or abandoned unit stays done in later runs. | In `Attempt`, set `okRead[u]` to `ok`, not `@ \/ ok`. | Action property violated |
| `NoRereadOfCompleteDay` | Action property | A run reads a complete day again only when it is due. | In `ScoreStep`, replace `Complete(u.day)` with `FALSE`. | Action property violated |
| `StoppedNeverMarks` | Action property | A run that stopped or had an error writes no marker. | On a stop, keep `clean` unchanged. Run with `MarkerOnlyWhenComplete` out of the `.cfg`, because that invariant fails first. | Action property violated |
| `ExitCodeRule` | Action property | Exit 1 if and only if a source failed or the budget stalled. Exit 75 only with no marker. Exit 0 only with the marker. | In `Finish`, test `clean` before `errors`. | Action property violated |
| `StallExitsOne` | Action property | A run that the budget stops with no finished unit exits 1. | In `EndNhl`, set `errors' = errors`. | Action property violated |
| `BacklogStallExitsOne` | Action property | A run that the budget stops with no finished backlog unit exits 1. An arrival does not count. | With `ArrivalsCount = TRUE` in the stall `.cfg`: the arrival counts as progress, so the run exits 75 and the property fails. | Action property violated (the daily-cadence stall, #186) |
| `BackfillFinishes` | Liveness | The marker appears eventually, when the budget covers the open days (`BudgetCovers`). | In `ScoreStep`, do not skip gaps. | Temporal property violated |
| `BackfillFinishes` | Liveness | Same | In the `.cfg`, set `OtherBroken = TRUE`. | Temporal property violated |
| `FinishesOrFails` | Liveness | Between runs, again and again, the marker exists or the last run exited 1. A budget that is too small ends in exit 1 in every run. | In `EndNhl`, set `errors' = errors`. Run with the stall `.cfg`, and with `StallExitsOne` out of the `.cfg`, because that property fails first. | Temporal property violated |
| `FinishesOrFails` | Liveness | Same | In `EndNhl`, raise the stall error only while `exitCode = 0`. Run with the stall `.cfg`, and with `StallExitsOne` out of the `.cfg`. One exit 1 and then endless exit 75 must fail. The older form `<>(marker \/ exitCode = 1)` passes this mutation. | Temporal property violated |

The plain run also checks for deadlock, so the CI command does not pass `-deadlock`.

## Findings

The model work found these facts.
The first one was a limit of the code. Issue #178 fixed it.

1. The budget must cover the open days and the due days.
   Each run reads every open day and every due day in window 1 before it reads an old day.
   If `BACKFILL_BUDGET_S` is too small for those reads, every run stops in window 1, and the backfill never finishes.
   With `OpenDays = {3, 4}` and `Budget = 2`, TLC finds this liveness failure.
   The spec states the condition as `BudgetCovers == Cardinality(OpenDays) < Budget`, and `BackfillFinishes` holds only under it.
   When the condition fails, the code now stops the run with exit 1 and a message that names `--budget-seconds` (#178).
   `specs/daily_prepare_stall.cfg` checks that rule with `StallExitsOne` and `FinishesOrFails`.
2. A failure of any source after the NHL steps blocks the marker, because `refresh` then exits 1.
   This is the rule "any failed step stops the script".
   A source that always fails also stops the plain refresh, so the result is a loud failure in both paths, and the spec models it as `OtherBroken`.
3. A boxscore gap is noted as a count, not by name.
   The note exists, so the gap is not dropped without a word.
4. The daily-cadence stall (#186).
   When an arrival counts as progress (`ArrivalsCount = TRUE`), a run can read an arrived day and then stop at an open day.
   That run exits 75 and never reaches the backlog.
   `BacklogStallExitsOne` fails in the stall `.cfg` with `ArrivalsCount = TRUE`, and it passes with the current rule.
   The current rule is the one that the code uses (`BACKLOG_BEFORE`, test `test_a_day_that_closed_since_the_last_run_does_not_count_as_progress`).
5. The reorder fix (`BacklogFirst = TRUE`) passes the stall `.cfg` and the full `.cfg`, both with the current rule.
   The full run took 32 minutes 49 seconds and 32,886,360 distinct states, against 29 minutes 21 seconds and 27,181,712 for the current order.
   The reorder count is the same as before the open-day fix (finding 6).
   In the full `.cfg`, the first tick closes the only open day, and a reorder run reaches that day only after day 1 is abandoned, which needs a tick.
   So the full `.cfg` under the reorder never reads the open day while it is open, and the stall `.cfg` is the check that does.
   It does not remove the need for the current rule.
   With `BacklogFirst = TRUE` and `ArrivalsCount = TRUE`, `BacklogStallExitsOne` still fails in the stall `.cfg`, because a run can count an arrival after the backlog is complete.
   The full `.cfg` with both settings was not run.
   The current counting rule is the fix. The reorder does not replace it and costs more states, so the code keeps its current order.
6. A read of a day that was still open completed the day once it closed (found in the review of PR #190).
   The model skipped the re-read after the close, which the code pays every day.
   `Attempt` now sets `okRead` only for a day that is closed, and `WindowDone` does not require an open day to be complete.
   The stall `.cfg` still passes with the current rule, and `ArrivalsCount = TRUE` still fails.
7. Arrivals expose a gap in `MarkerOnlyWhenComplete`.
   A run can write the marker while a day is open, and the day closes later with its boxscore unread.
   The code covers this with the plain refresh after the marker, so the invariant now checks the step that writes the marker.

## Files to add or change

1. `specs/daily_prepare.tla`: copy the reference spec from the appendix without change.
2. `specs/daily_prepare.cfg` and `specs/daily_prepare_stall.cfg`: copy the reference `.cfg` files from the appendix without change.
3. `scripts/tlc.sh`: the run script below.
4. `tests/test_tla_constants.py`: the constants test above.
5. `tests/test_tlc_script.py`: `--help` exits 0 and prints the usage, and an unknown argument exits 2 and names the argument. A wrong checksum deletes the jar and exits 1, and a failed download exits 1 and names the URL. A copy of the script, with the pinned hash of a fake jar, runs against a fake `java`: both configs pass, the first failure stops the run, and the exits 11, 12, 13, and an unrelated code are returned and named. The tests use a fake `curl`, a fake `java`, and a fake jar, so they do not download TLC.
The Java version checks run against fake `java` output for versions 8, 9, 11, 17, and 21, and for unreadable output.
A PATH without `sha256sum` and `shasum` exits 1 and names both.
6. `.github/workflows/tla.yml`: the CI job below.
7. `.editorconfig`: add a `[*.{tla,cfg}]` section with `indent_size = unset`. TLA+ aligns `/\` lists at any column, so the 2-space rule does not fit.
8. `AGENTS.md`: add `- Model check: bash scripts/tlc.sh (TLC on specs/daily_prepare.tla; needs Java 11 or later)` under Commands.
9. `CHANGELOG.md`: one `Added` entry.

The daily report does not change.
The spec is not an input source, a model, or a feature of the report, so the AGENTS.md report rule does not apply.

### `scripts/tlc.sh`

Behavior:

1. `set -euo pipefail`. A header comment states the usage, and `--help` prints it, as `scripts/daily-prepare.sh` does.
2. Flags: `--help` only. Any other argument exits 2 with a message that names it. The script passes `-workers auto`, because TLC runs one worker by default.
3. `TLA_TOOLS_VERSION="v1.7.4"` and `TLA_TOOLS_SHA256="936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"`.
   v1.7.4 is the newest release that is not a pre-release.
   The checksum came from the v1.7.4 `tla2tools.jar` release asset on 2026-11-07, because GitHub gives no digest for this asset.
4. The jar goes to `${TLA_TOOLS_DIR:-$HOME/.cache/hockey-model/tla}/tla2tools-$TLA_TOOLS_VERSION.jar`.
   If the file is missing, download `https://github.com/tlaplus/tlaplus/releases/download/$TLA_TOOLS_VERSION/tla2tools.jar` with `curl -fsSL` to a temporary name in that directory.
5. Check the SHA-256 on every run, not only after a download.
   Use `sha256sum` when it is on `PATH`, and `shasum -a 256` when it is not.
   On a mismatch, delete the file, print the expected and the actual sums, and exit 1.
6. Before any download, if `java` is not on `PATH`, exit 1 with "install Java 11 or later".
   Then read the major version from `java -version`.
   A major version below 11 exits 1 and names the version, because an old JDK fails later with a class-version error (#183).
   A version line that cannot be read exits 1.
   If neither `sha256sum` nor `shasum` is on `PATH`, exit 1 and name both tools.
7. Run TLC from the repository root:
   For `specs/daily_prepare_stall.cfg` first and then `specs/daily_prepare.cfg`, run `java -XX:+UseParallelGC -cp "$tmp/tla2tools.jar" tlc2.TLC -workers auto -metadir "$tmp/states/NAME" -config CFG specs/daily_prepare.tla`, on one private copy of the jar that the checksum covered. Stop at the first non-zero code.
   `$tmp` comes from `mktemp -d`, with a `trap` that removes it.
8. Exit with the TLC exit code.
   TLC exits 0 when every property holds, 12 for a violated invariant, and 13 for a violated action or temporal property.
   On a non-zero code, print one line to stderr that names the code and says that the trace is above.

Run `shellcheck` and `shfmt` on the script.

### `.github/workflows/tla.yml`

```yaml
name: tla

# TLC checks the daily prepare spec. It runs when the spec or the code that it models changes.
# It is not a required check, because the path filter skips most pull requests.
on:
  pull_request:
    paths:
      - specs/**
      - scripts/tlc.sh
      - scripts/daily-prepare.sh
      - src/hockey_model/store/refresh_backfill.py
      - src/hockey_model/store/refresh_nhl.py
      - src/hockey_model/store/refresh_context.py
      - src/hockey_model/store/source_reads.py
      - src/hockey_model/store/refresh.py
      - src/hockey_model/store_cli.py
      - .github/workflows/tla.yml
  push:
    branches: [main]
    paths:
      - specs/**
      - scripts/tlc.sh
      - scripts/daily-prepare.sh
      - src/hockey_model/store/refresh_backfill.py
      - src/hockey_model/store/refresh_nhl.py
      - src/hockey_model/store/refresh_context.py
      - src/hockey_model/store/source_reads.py
      - src/hockey_model/store/refresh.py
      - src/hockey_model/store_cli.py
      - .github/workflows/tla.yml
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: tla-${{ github.ref }}
  cancel-in-progress: true

jobs:
  tlc:
    runs-on: ubuntu-latest
    # The full config needs about 38 minutes on a runner: the search takes 29 minutes and the
    # liveness check follows. #188 tracks the reduction that lets this limit come down.
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: actions/setup-java@77dd5b2ce225ea11469928ac317850245a74ae78 # v6.2.0
        with:
          distribution: temurin
          java-version: "21"
      - env:
          TLA_TOOLS_DIR: ${{ runner.temp }}/tla
        run: bash scripts/tlc.sh
```

The two path lists MUST stay the same.
The checkout pin is the one in `ci.yml`.
The `setup-java` SHA is the `v6.2.0` tag commit, which GitHub published on 2026-11-04.
Run `actionlint` and `zizmor` on the file.

## Verification

1. `bash scripts/tlc.sh` passes locally, with 4,000,218 distinct states in the first model and 16,050 in the stall model.
2. Run each break test in "Properties" on a scratch copy of the spec, outside the repository.
   Confirm that TLC fails with the listed result.
   Do not commit the broken copies.
3. `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run ty check` pass.
4. `prek run --all-files` passes, which includes `editorconfig-checker`.
5. The `tla` workflow passes on the pull request. The run time is in the job log. If it is more than 20 minutes, report it in the pull request.

## Keep the spec in step

The mapping from spec to code stays manual.
A change to a rule in "What the code does" MUST update the spec and this note in the same pull request.
The path filter runs TLC on such a change.

## Non-goals

- The HTTP calls and the game model.
- A proof that the Python code matches the spec.
- The plain refresh, the backtest, the fit, and `prepared-DATE` after the marker exists. The model stops at the marker.
- The `--date` input. It names files and does not change the state machine.
- A job that the runner kills before the cache save, and cache eviction. Both drop a run or the whole store. The cache-scope work in #139 covers them.
- Due days in calm weather. The model assumes that a good re-read resolves every game.

## Semgrep scope (#182)

The nbl-dev Semgrep catalog has no TLA+ ruleset.
So the scan reports `unknown_type` for `specs/daily_prepare.tla` and skips it.
This gap is accepted.
The file is model text, and TLC reads it. Nothing in the repo runs it as code.
The catalog lives in the nbl-dev plugin, outside this repo, so a catalog entry is not a change this repo can make.

## Appendix: reference spec

TLC 1.7.4 checked this spec and these `.cfg` files with no violation.
Copy all three files byte for byte.

### `specs/daily_prepare.tla`

```tla
---------------------------- MODULE daily_prepare ----------------------------
(* A model of the backfill in scripts/daily-prepare.sh and store/refresh_backfill.py.
   Design and mapping to the code: docs/design/daily-prepare-tla.md *)
EXTENDS Naturals, FiniteSets

CONSTANTS
    NWindows,       \* windows in the backfill; window 1 is the newest
    DaysPerWindow,  \* score days in one window
    Budget,         \* attempts one run makes before out_of_time() is true
    MaxFails,       \* MAX_UNIT_FAILURES, scaled down
    AbandonReads,   \* ABANDON_AFTER_FAILED_READS
    AbandonTicks,   \* ticks in ABANDON_MIN_SPAN_DAYS
    RetryTicks,     \* ticks in RETRY_FAILED_AFTER
    OpenDays,       \* days that are not closed at the start; a tick closes them one by one
    BrokenScore,    \* score days whose every read fails
    BrokenBox,      \* days whose boxscore read always fails
    OtherBroken,    \* TRUE when a source after the NHL steps always fails
    ArrivalsCount,  \* TRUE: a day that closed since the last run counts as progress (old rule)
    BacklogFirst    \* TRUE: read the oldest window first, so the backlog comes before the open days

Days == 1..(NWindows * DaysPerWindow)
Kinds == {"score", "box"}
Urls == [kind : Kinds, day : Days]
Score(d) == [kind |-> "score", day |-> d]
Box(d) == [kind |-> "box", day |-> d]

ASSUME /\ NWindows >= 1 /\ DaysPerWindow >= 1 /\ Budget >= 1 /\ MaxFails >= 1
       /\ AbandonReads >= 1 /\ AbandonTicks >= 1 /\ RetryTicks >= 1
       /\ OpenDays \subseteq Days /\ BrokenScore \subseteq Days /\ BrokenBox \subseteq Days
       /\ OtherBroken \in BOOLEAN /\ ArrivalsCount \in BOOLEAN /\ BacklogFirst \in BOOLEAN

\* TRUE when a run can read every day that is open at the start and still have budget for one
\* more unit. Each tick only closes days, so the condition stays true after a tick.
BudgetCovers == Cardinality(OpenDays) < Budget

Min(a, b) == IF a < b THEN a ELSE b
Oldest(S) == CHOOSE d \in S : \A e \in S : d <= e

(* The work order of one run. Window 1 comes first when BacklogFirst is FALSE, and it comes last
   when BacklogFirst is TRUE. In a window, read_days takes the days oldest first, then
   read_boxscores takes the games oldest first. *)
Steps == NWindows * 2 * DaysPerWindow
WindowAt(p) == ((p - 1) \div (2 * DaysPerWindow)) + 1
OffsetAt(p) == (p - 1) % (2 * DaysPerWindow)
KindAt(p) == IF OffsetAt(p) < DaysPerWindow THEN "score" ELSE "box"
\* k is the window index with the oldest window as 1.
DayAt(p) == LET k == IF BacklogFirst THEN WindowAt(p) ELSE NWindows - WindowAt(p) + 1
            IN (k - 1) * DaysPerWindow + (OffsetAt(p) % DaysPerWindow) + 1
UnitAt(p) == [kind |-> KindAt(p), day |-> DayAt(p)]
StageStart(p) == OffsetAt(p) = 0 \/ OffsetAt(p) = DaysPerWindow
BoxStageOf(p) == (WindowAt(p) - 1) * 2 * DaysPerWindow + DaysPerWindow + 1
LastOfWindow(p) == OffsetAt(p) = 2 * DaysPerWindow - 1

VARIABLES
    okRead, streak, span, firstAge, lastAge,  \* source_reads, per URL
    marker,                                   \* the .backfilled file
    open,                                     \* days that are not closed yet
    arrived,                                  \* days that closed since the last run ended
    calm,                                     \* TRUE: every read that is not broken works
    phase, pc, gaps, noted, due, fails, reads,
    progress,  \* what the code counts: a finished unit, as ArrivalsCount sets it
    backlog,   \* ground truth: a finished unit that is not an arrival
    clean, stopped, errors,  \* one run
    exitCode

store == <<okRead, streak, span, firstAge, lastAge, marker, open, arrived>>
run == <<phase, pc, gaps, noted, due, fails, reads, progress, backlog, clean, stopped, errors>>
vars == <<store, calm, run, exitCode>>

\* abandoned_urls(): enough failed reads since the last good read, far enough apart.
Abandoned(u) == streak[u] >= AbandonReads /\ span[u]
\* _day_complete(): a good read after the day closed.
Complete(d) == okRead[Score(d)] /\ d \notin open
\* games_missing_boxscore(): the score read found a final game with no boxscore.
WantBox(d) == okRead[Score(d)] /\ d \notin open /\ ~okRead[Box(d)]
\* failed_since(now - RETRY_FAILED_AFTER) in read_boxscores.
RecentFail(u) == streak[u] > 0 /\ lastAge[u] < RetryTicks

\* A unit is done when a read worked or when it is a known gap. No later run undoes it.
Done(u) == okRead[u] \/ Abandoned(u)
ScoreDone(d) == Done(Score(d))
\* A box is needed only for a final game, and the score read finds the game.
BoxDone(d) == ~WantBox(d) \/ Abandoned(Box(d))
WindowDays(k) == {d \in Days : (NWindows - k) * DaysPerWindow < d
                                /\ d <= (NWindows - k + 1) * DaysPerWindow}
\* A day that is still open is not required to be complete: the model does not record a good
\* read of an open day, and a failed or skipped read leaves the run unclean, so no marker.
WindowDone(k) == \A d \in WindowDays(k) : (d \in open \/ ScoreDone(d)) /\ BoxDone(d)

Broken(u) == IF u.kind = "score" THEN u.day \in BrokenScore ELSE u.day \in BrokenBox

Init == /\ okRead = [u \in Urls |-> FALSE]
        /\ streak = [u \in Urls |-> 0]
        /\ span = [u \in Urls |-> FALSE]
        /\ firstAge = [u \in Urls |-> 0]
        /\ lastAge = [u \in Urls |-> 0]
        /\ marker = FALSE
        /\ open = OpenDays
        /\ arrived = {}
        /\ calm = FALSE
        /\ phase = "idle" /\ pc = 1 /\ gaps = {} /\ noted = {} /\ due = {}
        /\ fails = 0 /\ reads = 0 /\ progress = FALSE /\ backlog = FALSE
        /\ clean = TRUE /\ stopped = FALSE /\ errors = FALSE
        /\ exitCode = 0

TypeOK ==
    /\ okRead \in [Urls -> BOOLEAN]
    /\ streak \in [Urls -> 0..AbandonReads]
    /\ span \in [Urls -> BOOLEAN]
    /\ firstAge \in [Urls -> 0..AbandonTicks]
    /\ lastAge \in [Urls -> 0..RetryTicks]
    /\ marker \in BOOLEAN /\ calm \in BOOLEAN
    /\ open \subseteq Days /\ arrived \subseteq Days
    /\ phase \in {"idle", "nhl", "other", "finish"}
    /\ pc \in 1..(Steps + 1)
    /\ gaps \subseteq Urls /\ noted \subseteq Urls /\ due \subseteq Days
    /\ fails \in 0..MaxFails /\ reads \in 0..Budget
    /\ progress \in BOOLEAN /\ backlog \in BOOLEAN
    /\ clean \in BOOLEAN /\ stopped \in BOOLEAN /\ errors \in BOOLEAN
    /\ exitCode \in {0, 1, 75}

(* Time passes only between runs. One tick is ABANDON_MIN_SPAN_DAYS. A run takes much less
   time than one tick, so every read of a run has the same time. A tick also closes the oldest
   open day. That day is an arrival: it closed since the last run, and the code leaves it out
   of the backlog (BACKLOG_BEFORE). *)
Tick ==
    /\ phase = "idle"
    /\ firstAge' = [u \in Urls |-> IF streak[u] > 0
                                   THEN Min(firstAge[u] + 1, AbandonTicks) ELSE 0]
    /\ lastAge' = [u \in Urls |-> IF streak[u] > 0
                                  THEN Min(lastAge[u] + 1, RetryTicks) ELSE 0]
    /\ open' = IF open = {} THEN open ELSE open \ {Oldest(open)}
    /\ arrived' = IF open = {} THEN arrived ELSE arrived \cup {Oldest(open)}
    /\ UNCHANGED <<okRead, streak, span, marker, calm, run, exitCode>>

GoCalm ==
    /\ ~calm
    /\ calm' = TRUE
    /\ UNCHANGED <<store, run, exitCode>>

(* daily-prepare.sh runs a backfill only while the marker is missing. score_gaps() and
   read_boxscores() read the gaps before any read of this run, and note them. *)
StartRun ==
    /\ phase = "idle" /\ ~marker
    /\ phase' = "nhl"
    /\ gaps' = {u \in Urls : Abandoned(u)}
    /\ noted' = {u \in Urls : Abandoned(u)}
    \* In calm weather a re-read resolves every game, so no day is due again.
    /\ \E D \in SUBSET {d \in Days : okRead[Score(d)]} : due' = IF calm THEN {} ELSE D
    /\ UNCHANGED <<store, calm, pc, fails, reads, progress, backlog, clean, stopped, errors,
                   exitCode>>

\* A finished unit that the run counts. An arrival is not backlog, so the old rule (ArrivalsCount)
\* is the only case where an arrival counts as progress.
Finished(u, ok) == ok /\ ~okRead[u] /\ u.day \notin open
CountsAsProgress(u, ok) == Finished(u, ok) /\ (ArrivalsCount \/ u.day \notin arrived)
CountsAsBacklog(u, ok) == Finished(u, ok) /\ u.day \notin arrived

\* run.attempt(): one read in one transaction. It records the result in source_reads.
\* A read finishes a unit for good when it works and the unit had no good read before.
\* An open day never finishes, because the next run reads it again.
Attempt(u, ok) ==
    /\ reads' = reads + 1
    /\ progress' = (progress \/ CountsAsProgress(u, ok))
    /\ backlog' = (backlog \/ CountsAsBacklog(u, ok))
    \* A good read of a day that is still open does not complete it (day_is_complete needs a
    \* read after the close). streak and the ages still reset, as in source_reads.
    /\ okRead' = [okRead EXCEPT ![u] = @ \/ (ok /\ u.day \notin open)]
    /\ IF ok
       THEN /\ streak' = [streak EXCEPT ![u] = 0]
            /\ span' = [span EXCEPT ![u] = FALSE]
            /\ firstAge' = [firstAge EXCEPT ![u] = 0]
            /\ lastAge' = [lastAge EXCEPT ![u] = 0]
       ELSE /\ streak' = [streak EXCEPT ![u] = Min(@ + 1, AbandonReads)]
            /\ span' = [span EXCEPT ![u] = IF streak[u] = 0 THEN FALSE
                                           ELSE @ \/ firstAge[u] >= AbandonTicks]
            /\ firstAge' = [firstAge EXCEPT ![u] = IF streak[u] = 0 THEN 0 ELSE @]
            /\ lastAge' = [lastAge EXCEPT ![u] = 0]
    /\ UNCHANGED <<open, arrived>>

Outcomes(u) == IF Broken(u) THEN {FALSE} ELSE IF calm THEN {TRUE} ELSE BOOLEAN

\* Move to the next unit. A stop jumps to the box stage of the window, or ends the NHL steps.
Advance(stopNow, failsNext) ==
    /\ pc' = IF stopNow /\ KindAt(pc) = "score" THEN BoxStageOf(pc)
             ELSE IF stopNow THEN Steps + 1
             ELSE IF stopped /\ LastOfWindow(pc) THEN Steps + 1
             ELSE pc + 1
    /\ fails' = failsNext

\* One loop pass of read_days().
ScoreStep ==
    LET u == UnitAt(pc)
        f == IF StageStart(pc) THEN 0 ELSE fails
    IN \/ /\ f >= MaxFails
          /\ stopped' = TRUE /\ clean' = FALSE
          /\ Advance(TRUE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors>>
       \/ /\ f < MaxFails
          /\ u \in gaps \/ (u.day \notin due /\ Complete(u.day))
          /\ Advance(FALSE, 0)
          /\ UNCHANGED <<store, reads, progress, backlog, errors, clean, stopped>>
       \/ /\ f < MaxFails
          /\ ~(u \in gaps \/ (u.day \notin due /\ Complete(u.day)))
          /\ reads >= Budget
          /\ stopped' = TRUE /\ clean' = FALSE
          /\ Advance(TRUE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors>>
       \/ /\ f < MaxFails
          /\ ~(u \in gaps \/ (u.day \notin due /\ Complete(u.day)))
          /\ reads < Budget
          /\ \E ok \in Outcomes(u) :
                /\ Attempt(u, ok)
                /\ clean' = (clean /\ ok)
                /\ errors' = (errors \/ ~ok)
                /\ Advance(FALSE, IF ok THEN 0 ELSE Min(f + 1, MaxFails))
          /\ UNCHANGED <<marker, stopped>>

\* One loop pass of read_boxscores(). A game that is not waiting is not in its list.
BoxStep ==
    LET u == UnitAt(pc)
        f == IF StageStart(pc) THEN 0 ELSE fails
    IN \/ /\ ~WantBox(u.day) \/ u \in gaps
          /\ Advance(FALSE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors, clean, stopped>>
       \/ /\ WantBox(u.day) /\ u \notin gaps
          /\ f >= MaxFails \/ reads >= Budget
          /\ stopped' = TRUE /\ clean' = FALSE
          /\ Advance(TRUE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors>>
       \/ /\ WantBox(u.day) /\ u \notin gaps
          /\ f < MaxFails /\ reads < Budget
          /\ RecentFail(u)
          /\ clean' = FALSE
          /\ Advance(FALSE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors, stopped>>
       \/ /\ WantBox(u.day) /\ u \notin gaps
          /\ f < MaxFails /\ reads < Budget
          /\ ~RecentFail(u)
          /\ \E ok \in Outcomes(u) :
                /\ Attempt(u, ok)
                /\ clean' = (clean /\ ok)
                /\ errors' = (errors \/ ~ok)
                /\ Advance(FALSE, IF ok THEN 0 ELSE Min(f + 1, MaxFails))
          /\ UNCHANGED <<marker, stopped>>

NhlStep ==
    /\ phase = "nhl" /\ pc <= Steps
    /\ IF KindAt(pc) = "score" THEN ScoreStep ELSE BoxStep
    /\ UNCHANGED <<calm, phase, gaps, noted, due, exitCode>>

\* nhl() adds an error when the budget stopped the run before it finished one unit.
EndNhl ==
    /\ phase = "nhl" /\ pc = Steps + 1
    /\ phase' = "other"
    /\ errors' = (errors \/ (stopped /\ ~progress /\ reads >= Budget))
    /\ UNCHANGED <<store, calm, pc, gaps, noted, due, fails, reads, progress, backlog, clean,
                   stopped, exitCode>>

\* MoneyPuck, ESPN, and Daily Faceoff run after the NHL steps. A failure sets refresh exit 1.
OtherSources ==
    /\ phase = "other"
    /\ \E e \in (IF calm /\ ~OtherBroken THEN {FALSE}
                 ELSE IF OtherBroken THEN {TRUE} ELSE BOOLEAN) :
          errors' = (errors \/ e)
    /\ phase' = "finish"
    /\ UNCHANGED <<store, calm, pc, gaps, noted, due, fails, reads, progress, backlog, clean,
                   stopped, exitCode>>

\* The backfill branch of daily-prepare.sh: refresh_rc, then backfill_complete.
\* The run ends, so the arrivals of this run are no longer new for the next run.
Finish ==
    /\ phase = "finish"
    /\ exitCode' = IF errors THEN 1 ELSE IF clean THEN 0 ELSE 75
    /\ marker' = (~errors /\ clean)
    /\ arrived' = {}
    /\ phase' = "idle" /\ pc' = 1 /\ gaps' = {} /\ noted' = {} /\ due' = {}
    /\ fails' = 0 /\ reads' = 0 /\ progress' = FALSE /\ backlog' = FALSE
    /\ clean' = TRUE /\ stopped' = FALSE /\ errors' = FALSE
    /\ UNCHANGED <<okRead, streak, span, firstAge, lastAge, open, calm>>

Next == Tick \/ GoCalm \/ StartRun \/ NhlStep \/ EndNhl \/ OtherSources \/ Finish

Fairness == /\ WF_vars(GoCalm) /\ WF_vars(StartRun) /\ SF_vars(Tick)
            /\ WF_vars(NhlStep) /\ WF_vars(EndNhl) /\ WF_vars(OtherSources)
            /\ WF_vars(Finish)

Spec == Init /\ [][Next]_vars /\ Fairness

-----------------------------------------------------------------------------
\* Safety

\* Checked on the step that writes the marker. A day that is still open then is not required to
\* be complete. Once it closes, the plain refresh after the marker reads the day and its missing
\* boxscores (the non-backfill branch of nhl() reads every missing boxscore), so the model does
\* not require them in a later state.
MarkerOnlyWhenComplete == [][(~marker /\ marker') => \A k \in 1..NWindows : WindowDone(k)]_vars

AbandonedIsNoted == phase /= "idle" => gaps \subseteq noted

DoneStaysDone == [][\A u \in Urls : Done(u) => Done(u)']_vars

ScoreRead(d) == phase = "nhl" /\ reads' = reads + 1 /\ UnitAt(pc) = Score(d)
NoRereadOfCompleteDay == [][\A d \in Days : ScoreRead(d) => (~Complete(d) \/ d \in due)]_vars

Finishing == phase = "finish" /\ phase' = "idle"
StoppedNeverMarks == [][Finishing /\ (stopped \/ errors) => ~marker']_vars
ExitCodeRule == [][Finishing => /\ (exitCode' = 1 <=> errors)
                                 /\ (exitCode' = 75 => ~marker' /\ ~clean)
                                 /\ (exitCode' = 0 => marker')]_vars

\* A run that the budget stops with no finished unit exits 1, not 75.
StallExitsOne == [][Finishing /\ stopped /\ ~progress /\ reads >= Budget
                     => exitCode' = 1]_vars

\* The same rule, judged on the backlog alone. An arrival does not count here. With
\* ArrivalsCount TRUE, a run can count an arrival as progress and still exit 75 with no
\* backlog unit finished. This property fails then, and StallExitsOne does not. With
\* ArrivalsCount FALSE, progress and backlog are equal, so this property is the same check as
\* StallExitsOne. It is not an independent check. It exists as the target of that mutation.
BacklogStallExitsOne == [][Finishing /\ stopped /\ ~backlog /\ reads >= Budget
                              => exitCode' = 1]_vars

\* Liveness

\* A budget that covers the open days lets the backfill finish.
BackfillFinishes == BudgetCovers => <>marker

\* A budget that is too small ends in exit 1 in every run. Between runs, again and again,
\* the marker exists or the last run exited 1. One exit 1 and then endless exit 75 fails.
FinishesOrFails == []<>(marker \/ (phase = "idle" /\ exitCode = 1))
=============================================================================
```

### `specs/daily_prepare.cfg`

```text
SPECIFICATION Spec
CONSTANTS
    NWindows = 2
    DaysPerWindow = 2
    Budget = 2
    MaxFails = 1
    AbandonReads = 2
    AbandonTicks = 1
    RetryTicks = 2
    OpenDays = {4}
    BrokenScore = {1}
    BrokenBox = {3}
    OtherBroken = FALSE
    ArrivalsCount = FALSE
    BacklogFirst = FALSE
INVARIANTS
    TypeOK
    AbandonedIsNoted
PROPERTIES
    MarkerOnlyWhenComplete
    DoneStaysDone
    NoRereadOfCompleteDay
    StoppedNeverMarks
    ExitCodeRule
    StallExitsOne
    BacklogStallExitsOne
    BackfillFinishes
```

### `specs/daily_prepare_stall.cfg`

```text
SPECIFICATION Spec
CONSTANTS
    NWindows = 2
    DaysPerWindow = 1
    Budget = 1
    MaxFails = 1
    AbandonReads = 2
    AbandonTicks = 1
    RetryTicks = 2
    OpenDays = {2}
    BrokenScore = {}
    BrokenBox = {}
    OtherBroken = FALSE
    ArrivalsCount = FALSE
    BacklogFirst = FALSE
INVARIANTS
    TypeOK
    AbandonedIsNoted
PROPERTIES
    MarkerOnlyWhenComplete
    DoneStaysDone
    NoRereadOfCompleteDay
    StoppedNeverMarks
    ExitCodeRule
    StallExitsOne
    BacklogStallExitsOne
    FinishesOrFails
```
