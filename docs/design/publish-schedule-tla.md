# TLA+ model of the publish schedule

Tracks issue #241.
The rule for which module earns a spec is in [tla-scope.md](tla-scope.md), and the schedule itself is in [daily-report-triggers.md](daily-report-triggers.md).

This note is the full build plan.
An implementer follows "Build order", copies the reference spec and the two configs from the appendix byte for byte, adds the files in "Files to add or change", and runs the checks in "Verification".
TLC 1.7.4 already checked the reference spec and both configs with no violation, liveness included, and every break test in this note gives the listed result.

## Status

As of 2026-11-25 this note is a design only.
It is on the local branch `docs/241-264-tla-designs`.
The branch is not pushed, and it has no pull request.
The two bugs that the model finds are open issues #334 and #335.
On 2026-11-25 TLC 1.7.4 checked `specs/publish_schedule.tla` with `publish_schedule.cfg`: 566,044 distinct states, no error, in 14 s.
On that day `publish_schedule_half.cfg` also checked clean: 564,696 distinct states.
The reference spec and all three configs match the appendix byte for byte.
On that day the spot checks of "What the code does" matched the workflows and scripts: the cron minutes, the job groups and timeouts, the exit code 4, and the report reasons.

## Problem

`daily-prepare.yml` and `daily-report.yml` both run every hour (#282).
Three jobs share the concurrency group `hockey-model-store` with `cancel-in-progress: false`: the `skip` and `prepare` jobs of the prepare, and the `build` job of the report.
The `publish` job of the report has its own group for each day, `hockey-model-publish-DATE`.
The report gate decides from the time, the first puck drop, the prepare jobs that hold the group, the release, and a manual prepare run.
No test checks how a long prepare, the hourly triggers, the late start of a scheduled run, and the rule of the group interleave.
#162, #163, #239, #285, #295, #311, and #313 were each a timing interaction that a person found by hand.

The issue names two properties:

1. Safety: a day publishes at most once, except by a replace, and a day with no games never publishes.
2. Liveness: when the prepare finishes inside its timeout, a game day with games gets a report.

The issue names `scripts/trigger_hours.py`.
That script is gone: #282 replaced the list of trigger hours with an hourly cron, so the model has an hourly trigger and no list.

## The rule of a concurrency group

The issue asks to check the queue rule in the GitHub documentation first.
Two pages state it: [Control the concurrency of workflows and jobs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency) and the `concurrency` section of [Workflow syntax for GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
Both were read on 2026-11-25.

- At most one job of a group runs at a time.
- A group has a `queue` key. The default, `queue: single`, keeps at most one waiting job, and a newer waiting job cancels the older one.
- `queue: max` keeps up to 100 waiting jobs and cancels none until the queue is full. The jobs start in the order in which they began to wait, but the documentation says that the order is not guaranteed.
- `queue: max` together with `cancel-in-progress: true` is a validation error.

So the rule that the issue states is the rule of `queue: single`, which the workflows use today.
The `queue: max` option is newer than the design of these workflows, and the model checks both rules (finding 3).

## Options

1. Add the schedule to `specs/daily_prepare.tla`.
2. A separate spec of one game day, with the triggers, the gates, the jobs of the two groups, and the release, and with the time in ticks of 10 minutes.
3. A property test of the gate decision only.

## Choice

Option 2.
The backfill spec is already large (#188), and it models one prepare run and no report, so the schedule would multiply its states for no shared property.
The gate decision is a pure function, and #245 gives it an exhaustive table test, but the properties of the issue are about the order of jobs in time, which a test of one call cannot see.

The model keeps the state space small with three choices.
It has one game day with one first puck drop, a few hours of triggers before and after it, and no history of URLs or reads.
It counts the inputs of the report as a version number that changes at most `MaxInputs` times.
It makes each gate one step: the gate reads its facts, and its job joins the group in the same step.

## What the code does

The spec states these rules.
Each rule names the code that owns it.

### The prepare: `daily-prepare.yml`

1. The cron fires at minute 14 of every hour.
   A manual dispatch can set `date` and `force`.
2. The `gate` job has no group.
   On a scheduled run it calls `scripts/manual-prepare-pending.sh`, and the run stands aside while a manual `daily-prepare` run waits or runs (#285).
   A manual run never stands aside.
3. The `skip` job holds `hockey-model-store` with `timeout-minutes: 10`.
   It runs `daily-prepare.sh --check-only`: exit 0 for a prepared day, exit 4 for a day that needs the rebuild (#311).
   A forced run skips this job.
4. The `prepare` job holds the same group with `timeout-minutes: 180`.
   It runs when the skip job asks for a rebuild, or when the run is forced.
   `daily-prepare.sh` writes the `prepared-DATE` marker when the backfill, the backtest, and the fit worked.
   For a day that is already prepared and not forced, it ends in seconds (#281).

### The report gate: `daily-report.yml`, `scripts/report-gate.sh`, `report_gate.decide`

The cron fires at minute 44 of every hour, the value of `TRIGGER_MINUTE_UTC` in `report_trigger.py`.
The `gate` job has no group, so it starts at the trigger and can read the prepare jobs that hold the group (#163).

1. `scripts/prep-running.sh` prints the end of the window of each prepare job that runs or waits: its start plus its own timeout. A job that waits has not started, so its window starts now.
2. `report-due` gives `due`: the trigger time plus `REPORT_BUDGET_S` is before the first puck drop.
   It gives `prep_blocks_report`: a prepare window plus `REPORT_BUDGET_S` reaches the first puck drop.
3. A due run that a prepare blocks builds nothing (`prepare-wait`).
4. A day with no games builds nothing (`no-games`).
5. A day with no release builds and publishes, on time (`due`) or after the drop (`late`).
6. A day with a release builds again before the drop (`replace`), unless a manual prepare run waits or runs (`manual-wait`, #295).
   After the drop, a day with a release builds nothing (`already-published`).

### The build: `daily-report.sh`

The `build` job holds `hockey-model-store`.

1. A store with no `prepared-DATE` marker writes no report: exit 0 before the first puck drop, exit 1 after it.
2. With `--skip-unchanged`, which the workflow passes on every build that may publish, the script reads the release after it predicts.
   When the release is complete for its newest build and that build has the same `inputs_sha256`, it writes no `complete` marker, so nothing publishes.
   A release that is behind its newest build builds as usual.

### The publish: `scripts/publish-daily-report.sh`

The `publish` job runs in `hockey-model-publish-DATE` with the default queue, so a newer waiting publish cancels an older one (#313).
It adds the files of the build to the release, then the alias and the index.
After #334, it first publishes nothing for a day with a release when the first puck drop has passed, or when the newest published build has the same `inputs_sha256`.

## The model

### Time

One tick is 10 minutes, and an hour is 6 ticks.
A cron minute rounds down to the start of its tick: the prepare at minute 14 is tick 1 of the hour, and the report at minute 44 is tick 4.
`REPORT_BUDGET_S` is 1 tick, the `skip` timeout is 1, and the `prepare` timeout is 18.
The rounding keeps the gate answer of the code: a report at 22:44 with a drop at 23:00 is due (22:54 is before 23:00), and in ticks 16 + 1 is less than 18.

A build runs for `BuildTicks`, which is the margin.
A publish job ends at most `PublishTicks` after it starts, and it adds time that the margin does not cover.
A scheduled run starts at its slot or up to `MaxDelayTicks` later, and GitHub can drop up to `MaxDrops` runs.
The 6-tick delay is the 50 to 60 minutes of #216.
The runs of one workflow start in hour order.

A step that must happen in the current tick blocks the next tick (`Urgent`): a job at its timeout, a waiting job when the group is free, a trigger at its last tick, and the join of a rebuild job.
After the last trigger hour, the clock runs on only while a job holds or waits for a group (`Busy`), so every job ends inside the model.

### Mapping from the code to the spec

| Code | Spec |
| --- | --- |
| The scheduled prepare run | `PrepareRun` (the gate and the `skip` job), `PrepareDropped` |
| A manual `daily-prepare` dispatch with `force` | `ManualPrepare`, a `rebuild` job with `manual` set |
| `manual-prepare-pending.sh` | `ManualLive` |
| The `skip` job and its exit code | `SkipEnd`, which hands a `rebuild` job to `joining` for a day that is not prepared |
| The `prepare` job of the same run | `RebuildJoin`, then `RebuildEnd(ok)` |
| The `prepared-DATE` marker | `prepared` |
| The scheduled report run: `report-gate.sh`, `report-due`, `decide` | `ReportRun`, through `Builds`, `Due`, `Blocks`, `PrepEnds` |
| `prep-running.sh` | `PrepEnds` |
| The `build` job and `daily-report.sh` | `BuildEnd`, through `Unchanged` |
| The `publish` job and `publish-daily-report.sh` | `PubEnd`, through `PublishSkips` |
| `hockey-model-store` | `holder` and `queue`, through `Join` and `Free` |
| `queue: single` or `queue: max` | `QueueMax` |
| `hockey-model-publish-DATE` | `pubHolder` and `pubWaiting`, through `PubJoin` |
| The release of the day | `published`, `newest` (the inputs version of its newest build), and `count` (its builds) |
| A starter confirmed, or another read that changes the report | `InputsChange` |
| The schedule lists games | `games`, chosen at the start |

### Constants

| Constant | Code | Value |
| --- | --- | --- |
| `TickMinutes` | The model's unit | 10 |
| `PrepSlot` | The cron minute of `daily-prepare.yml`, 14, divided by the tick | 1 |
| `ReportSlot` | `TRIGGER_MINUTE_UTC`, 44, divided by the tick | 4 |
| `MarginTicks` | `REPORT_BUDGET_S` in `daily-report.yml`, 600 seconds | 1 |
| `SkipTimeoutTicks` | `timeout-minutes` of the `skip` job, 10 | 1 |
| `RebuildTimeoutTicks` | `timeout-minutes` of the `prepare` job, 180 | 18 |
| `BuildTicks` | The build fits in the margin | 1 |
| `PublishTicks` | The publish job | 1 |
| `DropTick` | The first puck drop: 3 hours in at :00, or 3 hours 30 minutes in at :30 | 18 or 21 |
| `PrepHours`, `ReportHours` | The trigger hours of the model | 3 and 5 |
| `MaxDelayTicks`, `MaxDrops` | The late start of a scheduled run (#216), and the dropped runs | 6 and 1 |
| `MaxInputs` | The changes of the inputs before the drop | 1 |
| `RebuildCanFail`, `Manual`, `QueueMax` | A failed rebuild, the manual dispatch, the queue rule of #335 | `TRUE` |
| `LastMarkerTick` | The marker of the liveness property, two report hours before the last | 16 |

| Config | `DropTick` | Distinct states | Depth | Time on 10 local workers |
| --- | --- | --- | --- | --- |
| `specs/publish_schedule.cfg` | 18 | 566,044 | 113 | 13 s to 20 s |
| `specs/publish_schedule_half.cfg` | 21 | 564,696 | 113 | 15 s |

## Properties

| Property | Kind | Meaning |
| --- | --- | --- |
| `TypeOK` | Invariant | The variables have their types. |
| `NoGamesNoPublish` | Invariant | A day with no games has no release. |
| `PublishNeedsMarker` | Invariant | A release exists only for a prepared day. |
| `ReplaceBeforeDrop` | Action | A build that lands on a day with a release lands before the first puck drop. |
| `ReplaceNewInputs` | Action | A build that lands on a day with a release has inputs that differ from the newest published build. |
| `ManualKept` | Action | A manual dispatch that waits for the group leaves it only when it starts, or for the build of a day with no release (#295). |
| `ReportComes` | Temporal | A day with games whose marker exists by `LastMarkerTick` gets a release. |

`ReplaceBeforeDrop` and `ReplaceNewInputs` together are "a day publishes at most once, except by a replace".
The properties read the release through `count`, and not through `PublishSkips`, so a change of `PublishSkips` cannot also change the property that checks it.
`ReportComes` needs `Spec` with weak fairness on `Next`, which the spec has.

## Findings

### 1. A replace can land after the first puck drop (#334)

The gate reserves `REPORT_BUDGET_S` for the run, but the report step is the only step that the budget limits.
The publish job comes after the build, and a build can also wait behind a store job that joined after the gate read the group.
The break `publish-checks-inputs-only` leaves out the puck-drop check of #334, and TLC stops at `ReplaceBeforeDrop`:

1. Tick 16 (22:44 for a drop at 23:00): the gate decides a replace, because 16 + 1 is less than 18.
2. The build runs from tick 16 to tick 17.
3. The publish job ends at tick 18, which is the drop.

The alias `report-DATE.json` then names a build in which every started game is `made_after_start`.

### 2. A build can publish the same inputs again (#334)

`daily-report.sh --skip-unchanged` compares the inputs at the end of the build, and it builds as usual when the release is behind its newest build.
A release is behind while a publish uploads.
The break `today-publish-checks-nothing` puts the publish job of the code today into the spec, and TLC stops at `ReplaceNewInputs`:

1. Tick 9: the report run of hour 0 starts 50 minutes late, and its build takes the group.
2. Tick 10: the report run of hour 1 starts on time, and its build waits.
3. Tick 10: the first build ends, and its publish job starts.
4. Tick 11: the second build ends while the first publish still runs, so the release is behind, and the second build goes to the publish group too.
5. Tick 11: both publish jobs end, and the release holds two builds with the same inputs.

The fix of #334 makes the publish job decide, because the publish group of the day is the one place where these steps run one at a time.

### 3. A scheduled prepare cancels a waiting manual dispatch (#335)

The break `today-single-queue` sets `QueueMax = FALSE`, which is the queue rule of the code today, and TLC stops at `ManualKept`:

1. Tick 3: a scheduled prepare starts 20 minutes late, passes its gate, and its `skip` job takes the group.
2. Tick 4: a report build joins and waits.
3. Tick 4: the skip job finds that the day needs the rebuild and ends. The build takes the group, and the rebuild job of the run is about to join.
4. Tick 4: the operator dispatches a forced prepare, and its job waits.
5. Tick 4: the rebuild job of the scheduled run joins, and with `queue: single` it cancels the manual job.

The check of #285 runs in the gate job only, so it cannot see a manual run that starts after it.
#335 names two fixes, and TLC accepts both with every property, liveness included:

1. `queue: max` on the store group of all three jobs, the recommended fix: 566,044 states.
   No job is ever cancelled, so this fix also removes the cancelled build that #311 accepts, and the reason for the stand-aside rules of #285 and #295.
2. `queue: single`, with a stand-aside check before the rebuild job joins (the variant `single-queue-with-rebuild-stand-aside`): 148,694 states.

The accepted configs hold fix 1.
If the owner picks fix 2, use the variant edit in "The edit of each break" in the spec, and set `QueueMax = FALSE` in both configs.

### What holds

With #334, both queue rules keep every publish property.
The variant `single-queue-without-manual` checks the queue rule of the code today with the publish checks of #334 and no manual dispatch: 37,136 states, no violation.
`ReportComes` holds in every accepted config and variant, so a day with games and a marker by `LastMarkerTick` always gets a release, on time or late.

No other interaction was found wrong.

## Build order

1. #334: the publish checks, with their tests.
2. #335: the queue rule, with its tests.
3. This note: the spec, the configs, the shards, and the tests in "Files to add or change".

The spec states the rule after #334 and #335.
TLC checks the spec, not the code, so a spec that lands first passes and states a rule that the code does not have yet.
Do step 3 only after steps 1 and 2 are on the branch.

## Break tests

Each break edits a scratch copy of `specs/publish_schedule.tla` and, when the table says so, of `specs/publish_schedule.cfg`, outside the repository.
Run each break with `-workers 1`, because with more workers TLC searches in parallel and can name another violated property first.
"The edit of each break" gives the exact old and new text of each edit.

| Break | The code change that it stands for | TLC stops with |
| --- | --- | --- |
| `today-publish-checks-nothing` | The publish job of the code today, `queue: single`, no manual dispatch | `ReplaceNewInputs` |
| `publish-checks-inputs-only` | #334 without the puck-drop check | `ReplaceBeforeDrop` |
| `publish-checks-drop-only` | #334 without the inputs check | `ReplaceNewInputs` |
| `today-single-queue` | The queue rule of the code today, before #335 | `ManualKept` |
| `gate-ignores-games` | The gate builds a day with no games | `NoGamesNoPublish` |
| `build-ignores-marker` | `daily-report.sh` does not check the marker | `PublishNeedsMarker` |
| `no-late-report` | The gate builds a day with no release only while it is due | `ReportComes`. TLC prints "Temporal properties were violated", with no name. |
| `stand-aside-prepare-ignores-manual` | Fix 2 of #335, with the gate check of #285 removed | `ManualKept` |
| `reach-a-replace` | Reachability: add `count < 2` to `TypeOK` | `TypeOK` |
| `reach-a-late-first-report` | Reachability: apply `ReplaceBeforeDrop` to the first build too | `ReplaceBeforeDrop` |
| `reach-two-waiting-jobs` | Reachability: add `Len(queue) < 2` to `TypeOK` | `TypeOK` |
| `reach-a-waiting-manual` | Reachability: no manual job waits | `TypeOK` |

Two variants must pass with no violation:

| Variant | What it checks | Distinct states |
| --- | --- | --- |
| `single-queue-with-rebuild-stand-aside` | Fix 2 of #335 | 148,694 |
| `single-queue-without-manual` | The queue rule of the code today, with #334 | 37,136 |

A reachability break shows that a config reaches a state that a property needs, so that property is not empty.
`reach-a-late-first-report` also shows that a late first report, which the design allows, happens in the model.

### The edit of each break

#### `today-publish-checks-nothing`

In `specs/publish_schedule.cfg`, replace `QueueMax = TRUE` with `QueueMax = FALSE`.

In `specs/publish_schedule.cfg`, replace `Manual = TRUE` with `Manual = FALSE`.

Replace:

```tla
PublishSkips == published /\ (now >= DropTick \/ newest = pubHolder.inputs)
```

With:

```tla
PublishSkips == FALSE
```

#### `publish-checks-inputs-only`

Replace:

```tla
PublishSkips == published /\ (now >= DropTick \/ newest = pubHolder.inputs)
```

With:

```tla
PublishSkips == published /\ newest = pubHolder.inputs
```

#### `publish-checks-drop-only`

Replace:

```tla
PublishSkips == published /\ (now >= DropTick \/ newest = pubHolder.inputs)
```

With:

```tla
PublishSkips == published /\ now >= DropTick
```

#### `today-single-queue`

In `specs/publish_schedule.cfg`, replace `QueueMax = TRUE` with `QueueMax = FALSE`.

The spec does not change.

#### `gate-ignores-games`

Replace:

```tla
Builds ==
    /\ games
```

With:

```tla
Builds ==
    /\ TRUE
```

#### `build-ignores-marker`

Replace:

```tla
    /\ IF prepared /\ ~Unchanged
```

With:

```tla
    /\ IF ~Unchanged
```

#### `no-late-report`

Replace:

```tla
    /\ \/ ~published                                  \* due, or late
```

With:

```tla
    /\ \/ ~published /\ Due                           \* due only
```

#### `stand-aside-prepare-ignores-manual`

In `specs/publish_schedule.cfg`, replace `QueueMax = TRUE` with `QueueMax = FALSE`.

Replace:

```tla
RebuildJoin ==
    /\ joining # None
    /\ Join(joining)
    /\ joining' = None
```

With:

```tla
RebuildJoin ==
    /\ joining # None
    /\ IF ~joining.manual /\ ManualLive THEN UNCHANGED <<holder, queue>> ELSE Join(joining)
    /\ joining' = None
```

Replace:

```tla
    /\ IF ManualLive THEN UNCHANGED <<holder, queue>> ELSE Join(Job("skip", FALSE))
```

With:

```tla
    /\ Join(Job("skip", FALSE))
```

#### `reach-a-replace`

Replace:

```tla
TypeOK ==
    /\ now \in
```

With:

```tla
TypeOK ==
    /\ count < 2
    /\ now \in
```

#### `reach-a-late-first-report`

Replace:

```tla
ReplaceBeforeDrop == [][(count > 0 /\ count' > count) => now < DropTick]_vars
```

With:

```tla
ReplaceBeforeDrop == [][count' > count => now < DropTick]_vars
```

#### `reach-two-waiting-jobs`

Replace:

```tla
TypeOK ==
    /\ now \in
```

With:

```tla
TypeOK ==
    /\ Len(queue) < 2
    /\ now \in
```

#### `reach-a-waiting-manual`

Replace:

```tla
TypeOK ==
    /\ now \in
```

With:

```tla
TypeOK ==
    /\ ~Waits(LAMBDA j : j.manual)
    /\ now \in
```

#### `single-queue-with-rebuild-stand-aside`

In `specs/publish_schedule.cfg`, replace `QueueMax = TRUE` with `QueueMax = FALSE`.

Replace:

```tla
RebuildJoin ==
    /\ joining # None
    /\ Join(joining)
    /\ joining' = None
```

With:

```tla
RebuildJoin ==
    /\ joining # None
    /\ IF ~joining.manual /\ ManualLive THEN UNCHANGED <<holder, queue>> ELSE Join(joining)
    /\ joining' = None
```

#### `single-queue-without-manual`

In `specs/publish_schedule.cfg`, replace `QueueMax = TRUE` with `QueueMax = FALSE`.

In `specs/publish_schedule.cfg`, replace `Manual = TRUE` with `Manual = FALSE`.

The spec does not change.

## Files to add or change

1. `specs/publish_schedule.tla`: copy the reference spec from the appendix without change.
2. `specs/publish_schedule.cfg` and `specs/publish_schedule_half.cfg`: copy the reference configs from the appendix without change.
3. `specs/shards.json`: add the two shards below after the `registry` shard, and after the `reads` shards of #264 when they exist, so the small shards run first.
4. `tests/test_tla_publish_schedule.py`: the constants test below.
5. `tests/test_tla_appendix.py`: check this note too. When #264 already made the test read a map from each note to its files, add this note to the map. Otherwise make that change as `docs/design/source-reads-tla.md` describes it.
6. `docs/design/tla-scope.md`: in the row of `report_gate.py` and `report_trigger.py`, add "The scheduling part is modelled in `specs/publish_schedule.tla` (#241)."
7. `docs/design/daily-report-triggers.md`: add one line under "Hourly schedule" that points to this note for the model of the schedule.
8. `.github/workflows/tla.yml`: when #264 has not yet changed the first line of the header comment, change it to "TLC checks the spec of each shard in specs/shards.json." The workflow itself does not change.
9. `AGENTS.md`: add `specs/publish_schedule.tla` to the list of specs in the "Model check" line.
10. `CHANGELOG.md`: one `Added` entry for the model of the publish schedule.

The daily report does not change.
The spec is not an input source, a model, or a feature of the report, so the report rule of `AGENTS.md` does not apply.

### The shards

```json
    {
      "name": "publish",
      "spec": "specs/publish_schedule.tla",
      "config": "specs/publish_schedule.cfg",
      "covers": [
        ".github/workflows/daily-prepare.yml",
        ".github/workflows/daily-report.yml",
        "scripts/daily-report.sh",
        "scripts/manual-prepare-pending.sh",
        "scripts/prep-running.sh",
        "scripts/publish-daily-report.sh",
        "scripts/report-gate.sh",
        "src/hockey_model/report_gate.py",
        "src/hockey_model/report_trigger.py",
        "tests/test_tla_publish_schedule.py"
      ]
    },
    {
      "name": "publish-half",
      "spec": "specs/publish_schedule.tla",
      "config": "specs/publish_schedule_half.cfg",
      "covers": [
        ".github/workflows/daily-prepare.yml",
        ".github/workflows/daily-report.yml",
        "scripts/daily-report.sh",
        "scripts/manual-prepare-pending.sh",
        "scripts/prep-running.sh",
        "scripts/publish-daily-report.sh",
        "scripts/report-gate.sh",
        "src/hockey_model/report_gate.py",
        "src/hockey_model/report_trigger.py",
        "tests/test_tla_publish_schedule.py"
      ]
    },
```

The `covers` list names each file that owns a rule of "What the code does", and the constants test that ties the configs to them.
`scripts/daily-prepare.sh` is not listed, because the model takes its exit codes and its marker from `specs/daily_prepare.tla` as given, and its shards already list it.
A change of any listed path, of the spec, or of a config starts both shards.

### `tests/test_tla_publish_schedule.py`

Follow `tests/test_tla_store_upgrade.py`, import `parse_constants` from `test_tla_constants`, and read the workflows with `job_text` from `tests/workflow_support.py`.

1. `CFGS`: the configs of every shard in `specs/shards.json` whose `spec` is `specs/publish_schedule.tla`. Fail when there are fewer than two.
2. `TICK_MINUTES = 10`, and every config has `TickMinutes = 10`.
3. `PrepSlot` is the minute of the one `cron: "M * * * *"` line of `daily-prepare.yml`, divided by `TICK_MINUTES` with the rest dropped.
4. `ReportSlot` is `TRIGGER_MINUTE_UTC // TICK_MINUTES`, and the cron minute of `daily-report.yml` is `TRIGGER_MINUTE_UTC`.
5. `MarginTicks` is the value of `REPORT_BUDGET_S: "N"` in `daily-report.yml` divided by `TICK_MINUTES * 60`. Fail, and name the value, when the division leaves a rest.
6. `SkipTimeoutTicks` and `RebuildTimeoutTicks` are the `timeout-minutes` of the `skip` and the `prepare` jobs of `daily-prepare.yml`, divided by `TICK_MINUTES`. Fail when a division leaves a rest.
7. `BuildTicks <= MarginTicks`, because the model assumes that a build fits in the margin, and `PublishTicks >= 1`, because the publish job is not free.
8. `MaxDelayTicks * TICK_MINUTES >= 60`, which covers the delays that #216 measured.
9. `DropTick % (60 // TICK_MINUTES)` over all configs is the set `{0, 3}`: one drop on the hour and one on the half hour.
10. `LastMarkerTick + 2 * (60 // TICK_MINUTES) <= ReportHours * (60 // TICK_MINUTES)`, so two report hours follow the marker of the liveness property.
11. `QueueMax` matches the workflows: it is `TRUE` exactly when each of the three `hockey-model-store` groups carries `queue: max`.
12. For each config: every name under `INVARIANTS` and `PROPERTIES` is defined in the spec. The config checks every zero-argument definition of the spec except these helpers: `HourTicks`, `MaxTime`, `None`, `PubNone`, `Free`, `PrepEnds`, `Due`, `Blocks`, `ManualLive`, `Builds`, `PrepOpen`, `ReportOpen`, `Fixed`, `PrepareRun`, `PrepareDropped`, `ManualPrepare`, `ReportRun`, `ReportDropped`, `SkipEnd`, `RebuildJoin`, `Unchanged`, `BuildEnd`, `PublishSkips`, `PubEnd`, `InputsChange`, `Urgent`, `Busy`, `Tick`, `Init`, `Next`, `Spec`.

## Verification

1. `bash scripts/tlc.sh --shard publish` and `bash scripts/tlc.sh --shard publish-half` pass, with the state counts in "Constants".
   `bash scripts/tlc.sh` runs every shard and passes.
2. Each break and each variant gives the listed result with `-workers 1`.
   Run them on a scratch copy outside the repository, and do not commit the broken copies.
3. `uv run pytest`, `uv run python3 scripts/check_per_module_coverage.py`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run ty check` pass.
4. `prek run --all-files` passes.
5. The `tla (publish)` and `tla (publish-half)` jobs pass on the pull request. Report their times in the pull request.

## Keep the spec in step

The mapping from the spec to the code stays manual.
A change to a rule in "What the code does" MUST update the spec, its break tests, and this note in the same pull request.
The two shards start TLC on such a change, and a new file that owns one of these rules goes into their `covers` lists in the same pull request.
Issue #336 plans a fingerprint lock that flags a changed rule before CI.
Until #336 lands, this section is the only guard against drift.

## Not modeled

- A dry run, and a report dispatch with `force`.
- An external clock for the report publish. No workflow has one today, so the report runs only on its hourly cron. #222 asked for it and is closed. With one, a dispatch and a late scheduled run are two close report runs, which is the case of finding 2.
- More than one game day, and the choice of the day by the gate.
- The time between the read of a gate and the join of its job. The model makes them one step, so the race of a scheduled gate and a manual dispatch in the same few seconds, which `daily-report-triggers.md` accepts, is not in the model.
- The fallback of `prep-running.sh` for a run whose gate job has not ended (the longest window from now). The model has no such state, because a gate is one step.
- Runner queue time, and the overhead of every job except the publish job.
- A cache miss, a failed lookup (the gate fails, and the next hour tries again), a publish that stops part way (a release that is behind), and a `skip` job that fails.
- More than one manual dispatch, a manual dispatch with no `force`, and a manual dispatch for another date.
- The order of waiting jobs. The model starts them in the order in which they joined, and GitHub says that the order is not guaranteed.
- Scheduled runs of one workflow that start out of hour order.

## Appendix: reference spec

TLC 1.7.4 checked this spec with both configs, and found no violation.
Copy all three files byte for byte.
`tests/test_tla_appendix.py` checks that each file and its copy here are equal.

### `specs/publish_schedule.tla`

```tla
---------------------------- MODULE publish_schedule ----------------------------
\* A model of the hourly prepare and report runs of one game day: the scheduled triggers, the
\* gate decisions, the jobs that share the hockey-model-store concurrency group, the publish
\* job, and the release. The mapping is in docs/design/publish-schedule-tla.md (#241).
\*
\* One tick is TickMinutes minutes. The cron minutes round down to the start of their tick.
\* The model states the rule of the code after #334 and #335. The accepted config holds the
\* recommended fix of #335, `queue: max`.

EXTENDS Naturals, FiniteSets, Sequences

CONSTANTS
    TickMinutes,          \* minutes in one tick
    PrepSlot,             \* the tick of the hour of the daily-prepare cron minute
    ReportSlot,           \* the tick of the hour of TRIGGER_MINUTE_UTC
    MarginTicks,          \* REPORT_BUDGET_S, in ticks
    SkipTimeoutTicks,     \* timeout-minutes of the skip job, in ticks
    RebuildTimeoutTicks,  \* timeout-minutes of the prepare job, in ticks
    BuildTicks,           \* the ticks that a build runs once it holds the group
    PublishTicks,         \* the most ticks that a publish job runs
    DropTick,             \* the tick of the first puck drop of the game day
    PrepHours,            \* the prepare trigger fires in hours 0 .. PrepHours - 1
    ReportHours,          \* the report trigger fires in hours 0 .. ReportHours - 1
    MaxDelayTicks,        \* the most ticks that GitHub starts a scheduled run late
    MaxDrops,             \* the most scheduled runs that GitHub drops
    MaxInputs,            \* the most changes of the report inputs before the puck drop
    RebuildCanFail,       \* TRUE lets a rebuild end with no prepared marker
    Manual,               \* TRUE allows one manual daily-prepare dispatch with force
    QueueMax,             \* TRUE gives the store group `queue: max`, FALSE the default `single`
    LastMarkerTick        \* the liveness property asks about a marker made by this tick

HourTicks == 60 \div TickMinutes
MaxTime == (ReportHours + 1) * HourTicks

ASSUME 60 % TickMinutes = 0 /\ PrepSlot < HourTicks /\ ReportSlot < HourTicks
ASSUME BuildTicks >= 1 /\ SkipTimeoutTicks >= 1 /\ RebuildTimeoutTicks >= 2
ASSUME MaxDelayTicks <= HourTicks

\* A job of the store group. `since` is the tick at which it took the group.
Job(k, m) == [kind |-> k, manual |-> m, since |-> 0]
None == Job("none", FALSE)
\* A publish job of the day. `inputs` is the inputs version of its build.
Pub(i) == [kind |-> "publish", inputs |-> i, since |-> 0]
PubNone == [kind |-> "none", inputs |-> 0, since |-> 0]

VARIABLES
    now,          \* the clock, in ticks
    games,        \* the schedule lists games for the day
    prepared,     \* the prepared-DATE marker is in the store
    inputs,       \* the version of what the report would say now
    holder,       \* the job that holds the store group
    queue,        \* the jobs that wait for the store group, oldest first
    joining,      \* the rebuild job of a run whose skip job ended, before it joins the group
    pubHolder,    \* the publish job that runs
    pubWaiting,   \* the one publish job that waits
    published,    \* the day has a release
    newest,       \* the inputs version of the newest published build
    prepNext,     \* the next hour whose prepare trigger has not fired
    reportNext,   \* the next hour whose report trigger has not fired
    drops,        \* the scheduled runs that GitHub dropped
    manualUsed,   \* the manual dispatch happened
    count         \* the builds that the release holds

vars == <<now, games, prepared, inputs, holder, queue, joining, pubHolder, pubWaiting,
          published, newest, prepNext, reportNext, drops, manualUsed, count>>

IsPrep(j) == j.kind \in {"skip", "rebuild"}
Timeout(k) == IF k = "skip" THEN SkipTimeoutTicks ELSE RebuildTimeoutTicks
Started(j) == [j EXCEPT !.since = now]

\* -------------------------------------------------------------------------------------------
\* The concurrency groups. One job runs. With the default `queue: single`, one job waits, and a
\* newer waiting job cancels the older one. With `queue: max`, the jobs wait in the order in
\* which they joined, and no job is cancelled. A job that waits starts when the holder ends.

Join(j) ==
    IF holder = None /\ queue = <<>>
    THEN holder' = Started(j) /\ queue' = <<>>
    ELSE holder' = holder /\ queue' = IF QueueMax THEN Append(queue, j) ELSE <<j>>

Free ==
    /\ holder' = IF queue = <<>> THEN None ELSE Started(Head(queue))
    /\ queue' = IF queue = <<>> THEN <<>> ELSE Tail(queue)

Waits(P(_)) == \E i \in DOMAIN queue : P(queue[i])

\* The publish group of the day keeps the default `queue: single`.
PubJoin(p) ==
    IF pubHolder = PubNone /\ pubWaiting = PubNone
    THEN pubHolder' = Started(p) /\ pubWaiting' = PubNone
    ELSE pubHolder' = pubHolder /\ pubWaiting' = p

\* -------------------------------------------------------------------------------------------
\* What the report gate reads.

\* prep-running.sh: the end of the window of each prepare job that runs or waits. A job that
\* waits has not started, so its window starts now.
PrepEnds ==
    (IF IsPrep(holder) THEN {holder.since + Timeout(holder.kind)} ELSE {})
    \cup {now + Timeout(queue[i].kind) : i \in {k \in DOMAIN queue : IsPrep(queue[k])}}

\* report-due: the run can finish before the puck drop.
Due == now + MarginTicks < DropTick
\* prep_blocks_report: a prepare window plus the margin reaches the puck drop.
Blocks == \E e \in PrepEnds : e + MarginTicks >= DropTick
\* manual-prepare-pending.sh: the manual run has a job that waits or runs.
ManualLive == holder.manual \/ joining.manual \/ Waits(LAMBDA j : j.manual)

\* report-gate.sh and report_gate.decide: the run starts a build.
Builds ==
    /\ games
    /\ ~(Due /\ Blocks)                               \* prepare-wait
    /\ \/ ~published                                  \* due, or late
       \/ published /\ Due /\ ~ManualLive             \* replace, unless manual-wait

\* -------------------------------------------------------------------------------------------
\* The scheduled triggers. Each hour fires once, at its slot or up to MaxDelayTicks late, or
\* GitHub drops it. The runs of one workflow start in hour order.

PrepSlotOf(h) == h * HourTicks + PrepSlot
ReportSlotOf(h) == h * HourTicks + ReportSlot
PrepOpen == prepNext < PrepHours /\ PrepSlotOf(prepNext) <= now
ReportOpen == reportNext < ReportHours /\ ReportSlotOf(reportNext) <= now

Fixed == <<now, games, inputs, pubHolder, pubWaiting, published, newest, count>>

\* The prepare gate: a scheduled run stands aside while the manual run waits or runs (#285).
PrepareRun ==
    /\ PrepOpen
    /\ prepNext' = prepNext + 1
    /\ IF ManualLive THEN UNCHANGED <<holder, queue>> ELSE Join(Job("skip", FALSE))
    /\ UNCHANGED <<Fixed, prepared, joining, reportNext, drops, manualUsed>>

PrepareDropped ==
    /\ PrepOpen /\ drops < MaxDrops
    /\ prepNext' = prepNext + 1 /\ drops' = drops + 1
    /\ UNCHANGED <<Fixed, prepared, holder, queue, joining, reportNext, manualUsed>>

\* A manual dispatch with force skips the skip job and never stands aside.
ManualPrepare ==
    /\ Manual /\ ~manualUsed
    /\ manualUsed' = TRUE
    /\ Join(Job("rebuild", TRUE))
    /\ UNCHANGED <<Fixed, prepared, joining, prepNext, reportNext, drops>>

ReportRun ==
    /\ ReportOpen
    /\ reportNext' = reportNext + 1
    /\ IF Builds THEN Join(Job("build", FALSE)) ELSE UNCHANGED <<holder, queue>>
    /\ UNCHANGED <<Fixed, prepared, joining, prepNext, drops, manualUsed>>

ReportDropped ==
    /\ ReportOpen /\ drops < MaxDrops
    /\ reportNext' = reportNext + 1 /\ drops' = drops + 1
    /\ UNCHANGED <<Fixed, prepared, holder, queue, joining, prepNext, manualUsed>>

\* -------------------------------------------------------------------------------------------
\* The jobs.

\* daily-prepare.sh --check-only: exit 0 for a prepared day, exit 4 for a rebuild.
SkipEnd ==
    /\ holder.kind = "skip"
    /\ Free
    /\ joining' = IF prepared THEN None ELSE Job("rebuild", holder.manual)
    /\ UNCHANGED <<Fixed, prepared, prepNext, reportNext, drops, manualUsed>>

\* The prepare job of the same run joins the group once the skip job ends.
RebuildJoin ==
    /\ joining # None
    /\ Join(joining)
    /\ joining' = None
    /\ UNCHANGED <<Fixed, prepared, prepNext, reportNext, drops, manualUsed>>

\* daily-prepare.sh writes the marker when the backfill, the backtest, and the fit worked. A job
\* that GitHub stops at its timeout writes none.
RebuildEnd(ok) ==
    /\ holder.kind = "rebuild"
    /\ now >= holder.since + 1
    /\ ~ok => RebuildCanFail
    /\ prepared' = (prepared \/ ok)
    /\ Free
    /\ UNCHANGED <<Fixed, joining, prepNext, reportNext, drops, manualUsed>>

\* daily-report.sh: a store with no marker publishes nothing. --skip-unchanged publishes nothing
\* when the release is complete for its newest build and that build has the same inputs.
Unchanged == published /\ pubHolder = PubNone /\ newest = inputs

BuildEnd ==
    /\ holder.kind = "build"
    /\ now = holder.since + BuildTicks
    /\ Free
    /\ IF prepared /\ ~Unchanged
       THEN PubJoin(Pub(inputs))
       ELSE UNCHANGED <<pubHolder, pubWaiting>>
    /\ UNCHANGED <<now, games, inputs, published, newest, count, prepared, joining, prepNext,
                   reportNext, drops, manualUsed>>

\* publish-daily-report.sh, after #334: inside the publish group it publishes nothing
\* for a day whose release has passed its first puck drop, or whose newest published build has
\* the same inputs.
PublishSkips == published /\ (now >= DropTick \/ newest = pubHolder.inputs)

PubEnd ==
    /\ pubHolder # PubNone
    /\ IF PublishSkips
       THEN UNCHANGED <<published, newest, count>>
       ELSE published' = TRUE /\ newest' = pubHolder.inputs /\ count' = count + 1
    /\ pubHolder' = IF pubWaiting = PubNone THEN PubNone ELSE Started(pubWaiting)
    /\ pubWaiting' = PubNone
    /\ UNCHANGED <<now, games, inputs, prepared, holder, queue, joining, prepNext,
                   reportNext, drops, manualUsed>>

\* A starter is confirmed, or another read changes what the report says.
InputsChange ==
    /\ inputs < MaxInputs /\ now < DropTick
    /\ inputs' = inputs + 1
    /\ UNCHANGED <<now, games, prepared, holder, queue, joining, pubHolder, pubWaiting,
                   published, newest, count, prepNext, reportNext, drops, manualUsed>>

\* -------------------------------------------------------------------------------------------
\* The clock. A tick waits for each step that must happen in the current tick.

Urgent ==
    \/ holder.kind = "skip" /\ now = holder.since + SkipTimeoutTicks
    \/ holder.kind = "rebuild" /\ now = holder.since + RebuildTimeoutTicks
    \* A rebuild that is not forced ends at once for a day that is prepared (#281).
    \/ holder.kind = "rebuild" /\ ~holder.manual /\ prepared
       /\ now = holder.since + SkipTimeoutTicks
    \/ holder.kind = "build" /\ now = holder.since + BuildTicks
    \/ joining # None
    \/ pubHolder # PubNone /\ now = pubHolder.since + PublishTicks
    \/ PrepOpen /\ now = PrepSlotOf(prepNext) + MaxDelayTicks
    \/ ReportOpen /\ now = ReportSlotOf(reportNext) + MaxDelayTicks

\* After the last trigger hour the clock runs on only while a job holds or waits for a group.
Busy == holder # None \/ queue # <<>> \/ joining # None \/ pubHolder # PubNone

Tick ==
    /\ now < MaxTime \/ Busy
    /\ ~Urgent
    /\ now' = now + 1
    /\ UNCHANGED <<games, prepared, inputs, holder, queue, joining, pubHolder, pubWaiting,
                   published, newest, count, prepNext, reportNext, drops, manualUsed>>

Init ==
    /\ now = 0 /\ games \in BOOLEAN /\ prepared \in BOOLEAN /\ inputs = 0
    /\ holder = None /\ queue = <<>> /\ joining = None
    /\ pubHolder = PubNone /\ pubWaiting = PubNone
    /\ published = FALSE /\ newest = 0 /\ count = 0
    /\ prepNext = 0 /\ reportNext = 0 /\ drops = 0 /\ manualUsed = FALSE

Next ==
    \/ PrepareRun \/ PrepareDropped \/ ManualPrepare \/ ReportRun \/ ReportDropped
    \/ SkipEnd \/ RebuildJoin \/ RebuildEnd(TRUE) \/ RebuildEnd(FALSE)
    \/ BuildEnd \/ PubEnd \/ InputsChange \/ Tick

Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

\* -------------------------------------------------------------------------------------------
\* Properties. They read the release through `count`, the number of builds that it holds, and
\* not through PublishSkips, so that a change of PublishSkips shows as a violation.

TypeOK ==
    /\ now \in 0 .. MaxTime + (PrepHours + 1) * (RebuildTimeoutTicks + 1) + 2 * ReportHours
    /\ games \in BOOLEAN /\ prepared \in BOOLEAN
    /\ inputs \in 0 .. MaxInputs /\ newest \in 0 .. MaxInputs /\ published \in BOOLEAN
    /\ count \in 0 .. ReportHours
    /\ holder.kind \in {"none", "skip", "rebuild", "build"}
    /\ \A i \in DOMAIN queue : queue[i].kind \in {"skip", "rebuild", "build"}
    /\ QueueMax \/ Len(queue) <= 1
    /\ joining.kind \in {"none", "rebuild"}
    /\ drops \in 0 .. MaxDrops

\* A day with no games never publishes.
NoGamesNoPublish == ~games => count = 0

\* A release exists only for a prepared day.
PublishNeedsMarker == count > 0 => prepared

\* A build that lands on a day with a release is a replace: it lands before the first puck drop,
\* and its inputs differ from the inputs of the newest published build.
ReplaceBeforeDrop == [][(count > 0 /\ count' > count) => now < DropTick]_vars

ReplaceNewInputs == [][(count > 0 /\ count' > count) => newest' # newest]_vars

\* A manual dispatch that waits for the group is cancelled only by the build of a day with no
\* release, because a day with no report is worse than a delayed rebuild (#295).
ManualKept ==
    [][(/\ Waits(LAMBDA j : j.manual)
        /\ ~\E i \in DOMAIN queue' : queue'[i].manual
        /\ ~holder'.manual)
       => (queue' # <<>> /\ queue'[Len(queue')].kind = "build" /\ count = 0)]_vars

\* When the day has games and a rebuild makes the marker by LastMarkerTick, the day gets a
\* release.
ReportComes == games => ((<>(prepared /\ now <= LastMarkerTick)) => <>(count > 0))

==================================================================================================
```

### `specs/publish_schedule.cfg`

```text
SPECIFICATION Spec
CHECK_DEADLOCK FALSE

CONSTANTS
    TickMinutes = 10
    PrepSlot = 1
    ReportSlot = 4
    MarginTicks = 1
    SkipTimeoutTicks = 1
    RebuildTimeoutTicks = 18
    BuildTicks = 1
    PublishTicks = 1
    DropTick = 18
    PrepHours = 3
    ReportHours = 5
    MaxDelayTicks = 6
    MaxDrops = 1
    MaxInputs = 1
    RebuildCanFail = TRUE
    Manual = TRUE
    QueueMax = TRUE
    LastMarkerTick = 16

INVARIANTS
    TypeOK
    NoGamesNoPublish
    PublishNeedsMarker

PROPERTIES
    ReplaceBeforeDrop
    ReplaceNewInputs
    ManualKept
    ReportComes
```

### `specs/publish_schedule_half.cfg`

```text
SPECIFICATION Spec
CHECK_DEADLOCK FALSE

CONSTANTS
    TickMinutes = 10
    PrepSlot = 1
    ReportSlot = 4
    MarginTicks = 1
    SkipTimeoutTicks = 1
    RebuildTimeoutTicks = 18
    BuildTicks = 1
    PublishTicks = 1
    DropTick = 21
    PrepHours = 3
    ReportHours = 5
    MaxDelayTicks = 6
    MaxDrops = 1
    MaxInputs = 1
    RebuildCanFail = TRUE
    Manual = TRUE
    QueueMax = TRUE
    LastMarkerTick = 16

INVARIANTS
    TypeOK
    NoGamesNoPublish
    PublishNeedsMarker

PROPERTIES
    ReplaceBeforeDrop
    ReplaceNewInputs
    ManualKept
    ReportComes
```
