# TLA+ model of the read log and the as-of views

Tracks issue #264.
The rule for which module earns a spec is in [tla-scope.md](tla-scope.md).

This note is the full build plan.
An implementer follows "Build order", copies the reference spec and the two configs from the appendix byte for byte, adds the files in "Files to add or change", and runs the checks in "Verification".
TLC 1.7.4 already checked the reference spec and both configs with no violation, and every break test in this note fails as listed.

## Status

As of 2026-11-25 this note is a design only.
It is on the local branch `docs/241-264-tla-designs`.
The branch is not pushed, and it has no pull request.
The two bugs that the model finds are open issues #329 and #330.
On 2026-11-25 TLC 1.7.4 checked `specs/source_reads.tla` with `source_reads.cfg`: 147,515 distinct states, no violation.
On that day `source_reads_moneypuck.cfg` also checked clean: 2,604 distinct states.
The reference spec and both configs match the appendix byte for byte.
The 2026-11-20 implementation-spec comment on #264 is superseded by this note.
That comment is not updated yet.

## Problem

Four modules decide which stored observations a reader may use, and their rules interact.

- `store/source_reads.py` records each read and decides when a URL is a known gap.
- `store/staleness.py` judges each URL at a prediction cutoff by its newest read and by age.
- `store/reads.py` answers "the newest read before this cutoff", and it drops an injury snapshot that is too old.
- `store/membership.py` decides which goalies left a franchise, from the games that they played.

Each module has unit tests of one call.
No test checks a sequence of runs, and the properties that matter are about a sequence:

1. A good read clears a failure.
2. A gap stays a gap until a good read.
3. A retired failure stays out of the notes.
4. No answer at a cutoff uses a row fetched after the cutoff.
5. A membership answer depends only on the games that the cutoff includes.

## Options

1. One TLA+ model of all four modules and `store/moneypuck_gaps.py`, as the issue proposes.
2. A TLA+ model of the read log and the as-of views, and a Hypothesis property test of goalie membership.
3. Hypothesis tests only.

## Choice

Option 2.

The read log passes the three conditions of `tla-scope.md`.
Each run appends rows, and the gap rule and the as-of answers change with the log.
The properties span runs, and a wrong answer reads back clean.
TLC checks every log up to a bound, and it found the defects in "Findings", which the example tests did not find.

Goalie membership fails the first condition of `tla-scope.md`.
`goalie_membership_before` is a pure function of the stored games, the cutoff, and the team.
It keeps no state, and its answer does not depend on the order in which the games arrived.
A property test checks a pure function at a low cost, and `tests/store/test_membership.py` already holds one.
"Goalie membership" below extends that test.

`store/moneypuck_gaps.py` is in neither part.
`unmatched_moneypuck` counts the newest MoneyPuck rows that match no stored game, with no cutoff.
`mp_form_lag` reads results by game start, not by fetch time.
Neither function reads the read log, the issue names no sequence property for them, and `tests/store/test_moneypuck_gaps.py` covers them.

Option 1 multiplies the read-log states by every set of stored games, for a part with no state of its own.
The result delay of membership (`RESULT_DELAY`, 3 hours) also does not fit the 12-hour tick of the read log.
Option 3 samples logs at random, and TLC enumerates all of them up to the bound.

## The past bugs

The issue names #126, #127, #130, and #131 as bugs in these interactions.
Only #126 is in these modules.

| Issue | What it was | Where it is checked |
| --- | --- | --- |
| #126 | No command read a skipped gap again | `GapSticky`. A read of a dated gap in the model stands for `refresh --retry-skipped`. |
| #127 | The report gate decided in shell | Not in these modules. #241 and #245 cover the gate. |
| #130 | A boxscore with no goalie rows was read on every refresh | Not in these modules. The refresh worklist, `games_missing_boxscore()`, owns it. |
| #131 | `_day_complete` measured from the wrong time | Not in these modules. `store/refresh_nhl.py` owns it, and `specs/daily_prepare.tla` models it. |

These past bugs are in these modules, and each one is a break test below.

| Commit | Bug | Break test |
| --- | --- | --- |
| a49199c (#80) | A good read and a failed read at one fetch time came back in any order | `tie-takes-first-row` |
| a16421b (#65) | A good read of one URL cleared a failed read of another URL of the same source | `judged-by-newest-read-of-source` |
| a16421b (#65) | An injury snapshot of any age was used | `snapshot-has-no-age-limit` |
| 14000c6 (#279) | A streak of permanent failures waited for the span | `permanent-streak-waits-for-span` |
| 4880898 (#217) | A gap of a training season with a recent failure was named on every game row | `recent-branch-before-gap-gate` |
| 5525284 (#87) | Membership: a later game, a trade back, and two franchises at one start | The Hypothesis breaks in "Goalie membership" |

## What the code does

The rules below describe the code as it is on 2026-11-25.
Where a fix is pending, the item gives the code today and then the target rule, which the spec states.
Each rule names the code that owns it.
The prose names the constants and does not repeat their values, because the values live in the code.

### The read log: `store/source_reads.py`

1. `record_read()` and `record_failed_read()` append one row to `source_reads` for each read: the source, the URL, `fetched_at`, `ok`, the error, and `permanent`.
   No code changes or deletes a row.
2. `record_failed_read()` stores `permanent = 1` when the failure is a `SourceError` with a status that a retry cannot change (`PERMANENT_STATUSES` in `sources/http.py`), and 0 for any other failure.
   A row from a store before version 6 holds NULL, which counts as transient.
3. A refresh takes one stamp for each unit of work.
   The production clock has microseconds, so two rows rarely share a stamp, but a refresh with no clock gives every row of the run one stamp.
   The code orders the rows of one URL by `(fetched_at, rowid)`: `failed_since()`, `failed_urls()`, and `_latest_reads()` do so (a49199c).
4. `known_gaps()` holds the gap rule, and `abandoned_urls()` returns its keys.
   Today, the streak of a URL is its failed rows with a `fetched_at` after its newest good read.
   The URL is a gap when the streak has at least `ABANDON_AFTER_FAILED_READS` rows, and either they span at least `ABANDON_MIN_SPAN_DAYS` or every row is permanent.
   Target (#329): the streak is its failed rows that have no good row after them in `(fetched_at, rowid)` order.
   A URL is also a gap when at least `ABANDON_AFTER_FAILED_READS` rows of the streak are permanent, whatever the other rows are.
5. A refresh skips a dated URL that is a gap (`RefreshRun.skipped()`), unless `--retry-skipped` is set.
   A live feed (`LIVE_FEEDS`) is read on every run.
   A failed read of a live URL that was a gap before the read is not an error (`_attempt_live()` in `store/refresh_other.py`, #276).
   Any other failed read is an error, and the refresh exits 1.

### The as-of view: `store/staleness.py`

`stale_sources(conn, as_of)` returns each reason that a source is not current at the cutoff `as_of`.
`NhlGameModel.predict()` passes `min(as_of, game.start)`, so a started game and every backtest prediction ask about a past cutoff.

1. `_latest_reads()` takes the rows with `fetched_at < as_of`.
   For each URL it keeps the newest row and the newest good row, in `(fetched_at, rowid)` order.
2. Today, the gap set is `abandoned_urls(conn, source)`, which reads the whole log and has no cutoff, so a gap that formed after `as_of` can count (finding 3).
   Target (#330): the gap set is `known_gaps(conn, source, before=as_of)`, the gaps of the rows before the cutoff.
3. A source with no good read before the cutoff, in any of its URLs, has one reason, "no read of it has worked yet", and no reason for each URL.
4. Each URL with a row before the cutoff gets at most one reason from `_url_problem()`:
   - When the newest row failed, a URL with an age limit counts. `_age_limit()` gives a limit to the single feeds (`espn_injuries`, `daily_faceoff`) and to the current MoneyPuck files.
   - When the newest row failed, a gap counts when its season can move a prediction (`_changes_prediction()`). A gap of an older training season does not count, and `training_gaps()` lists it in the footer.
   - When the newest row failed, any other URL counts while the failure is at most `ACTIVE_FAILURE_WINDOW` old at the cutoff. After that the failure is retired.
   - When the newest row worked, a URL with an age limit counts when its newest good read is more than the limit old: `LIVE_FEED_MAX_AGE` for a single feed, and `MONEYPUCK_MAX_AGE` for a MoneyPuck file.
5. A daily source (`_DAILY_FEEDS`: `nhl_scores` and `espn_starters`) has one more reason when the newest good read of all its URLs is more than `LIVE_FEED_MAX_AGE` old (`_daily_problem()`).
6. MoneyPuck has one more reason for each current file with no row before the cutoff.
   The model has one MoneyPuck file, so there this case is item 3.

`training_gaps(conn, as_of)` lists each gap of a training season once, for the report footer.
Today it reads the whole log, as item 2 of `stale_sources()` does (finding 3).
Target (#330): it reads the gaps of the rows before `as_of`.

### The injury snapshot: `store/reads.py`

`injury_snapshot_before()` returns the newest good read of `espn_injuries` with `fetched_at < before`.
`injury_reads_before()` uses that snapshot while it is at most `LIVE_FEED_MAX_AGE` old, and it returns no injuries when the snapshot is older.
`stale_sources()` names the feed in that case.

## The model

### Time

One tick is 12 hours.
It is the largest unit that divides every window of the rules: `ABANDON_MIN_SPAN_DAYS` is 1 tick, `LIVE_FEED_MAX_AGE` is 2, `ACTIVE_FAILURE_WINDOW` is 14, and `MONEYPUCK_MAX_AGE` is 28.
A read happens at a tick from 0 to `MaxTime`, and several reads can share one tick, which is two rows with one fetch time.
The cutoff is not a variable.
Each property asks about every cutoff from 0 to `MaxCutoff`, so a cutoff far after the last read costs no states, and a short log reaches the long windows.

The clock never runs back.
So the row order of the log is the `(fetched_at, rowid)` order of the SQL, and the rows that a cutoff sees are a prefix of the log.

### The URLs

| Name | Stands for | Source | Rules that it exercises |
| --- | --- | --- | --- |
| `live` | The one URL of `espn_injuries` | Single feed | `LIVE_FEED_MAX_AGE`, the injury snapshot, permanent failures, a live gap |
| `day` | A score day of the current season | `nhl_scores` | The active window, a gap on the game rows, the daily rule |
| `old` | A score day of an old training season | `nhl_scores` | A gap in the footer |
| `mp` | A current MoneyPuck file | `moneypuck` | `MONEYPUCK_MAX_AGE` |

`day` and `old` share one source, so the model checks that a read of one URL does not change the answer for the other (#65).

### Mapping from the code to the spec

| Code | Spec |
| --- | --- |
| The `source_reads` rows | `log`, a sequence of records `[url, t, kind]`, where `kind` is "ok", "transient", or "permanent" |
| `record_read()`, `record_failed_read()` | `Read(u, k)` |
| The clock | `Tick` and `now` |
| `ORDER BY fetched_at DESC, rowid DESC LIMIT 1` | `Newest(u, L)` |
| `known_gaps()`, `abandoned_urls()` | `IsGap(u, L)`, `Gaps(L)` |
| `fetched_at < cutoff` | `Before(c, L)` |
| `_url_problem()` | `UrlReasons(u, c, B, gaps)` |
| `stale_sources()` | `Stale(c, L)`, through `StaleFrom(c, B, gaps)` |
| `training_gaps()` | `Footer(c, L)` |
| `_changes_prediction()` is False | `Training` |
| `_age_limit()` | `AgeLimited`, `Limit(u)` |
| `_DAILY_FEEDS` | `DailySources` |
| The age check of `injury_reads_before()` | `SnapshotUsed(c, L)` |
| `--retry-skipped`, and the read of a live feed on every run | A `Read` of a URL in `Gaps(log)` |

A reason is a record `[at, why]`.
`at` is the URL for a reason of one URL, and the source name for a reason of a source.
`why` is "failed", "old", "never", or "daily".

### Constants

| Constant | Code | Value |
| --- | --- | --- |
| `AbandonReads` | `ABANDON_AFTER_FAILED_READS` | 2 |
| `SpanTicks` | `ABANDON_MIN_SPAN_DAYS` | 1 |
| `LiveMaxAgeTicks` | `LIVE_FEED_MAX_AGE` | 2 |
| `ActiveWindowTicks` | `ACTIVE_FAILURE_WINDOW` | 14 |
| `MoneyPuckMaxAgeTicks` | `MONEYPUCK_MAX_AGE` | 28 |

Both configs hold the same values.
The bounds differ:

| Config | `Urls` | `PermanentUrls` | `MaxReads` | `MaxTime` | `MaxCutoff` | Distinct states | Time on 10 local workers |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `specs/source_reads.cfg` | live, day, old | live | 4 | 3 | 18 | 147,515 | 1 min 39 s to 2 min 13 s |
| `specs/source_reads_moneypuck.cfg` | live, mp | none | 3 | 3 | 33 | 2,604 | 3 s |

`MaxCutoff` must be at least the longest window of the config plus `MaxTime` plus 1, so that a read at the last tick ages past the window inside the range.
The constants test checks that bound.
The main config leaves out `mp` and the MoneyPuck config leaves out `day` and `old`, because each URL adds a factor of states, and `mp` needs the long range of cutoffs.

## Properties

| Property | Kind | Meaning |
| --- | --- | --- |
| `TypeOK` | Invariant | The log and the clock have their types. |
| `GapNeedsFailures` | Invariant | The last `AbandonReads` rows of a gap all failed. |
| `LastReadsMakeGap` | Invariant | A URL whose last `AbandonReads` rows failed is a gap when they span `SpanTicks` or all of them are permanent. |
| `GoodReadClears` | Invariant | A URL whose last row worked is no gap, and no later cutoff names it as failed. |
| `NoRowAfterCutoff` | Invariant | The answer at a cutoff is the answer from the rows fetched before it. |
| `RecentFailureNamed` | Invariant | A failure inside `ActiveWindowTicks` is named, unless the URL is a gap of a training season. |
| `GapNamedOnce` | Invariant | A dated gap is named once: in the footer for a training season, else on the game rows. |
| `AgeStaysNamed` | Invariant | A URL with an age limit that is named at one cutoff is named at the next, while no new read arrives. |
| `SnapshotAgrees` | Invariant | An injury snapshot in use is not named as old, and a snapshot that is not used is named. |
| `GapSticky` | Action | A gap stays a gap until a good read of that URL. |
| `AnswerFixed` | Action | After the clock passes a cutoff, no later read and no tick changes the answer at that cutoff. |

The properties of the issue map to these:

| The issue says | Property |
| --- | --- |
| A good read clears the failure | `GoodReadClears` |
| A gap stays a gap until a retry succeeds | `GapSticky` |
| A retired failure stays out of the notes | `AnswerFixed`, `RecentFailureNamed` |
| No answer uses a row fetched after its cutoff | `NoRowAfterCutoff`, `AnswerFixed` |
| The membership answer is the same whoever asks at the same cutoff | The Hypothesis test in "Goalie membership" |

A retired failure can only come back through a later row, which `AnswerFixed` forbids.
At a later cutoff with no new read, the age of the failure only grows and the gap set of the rows does not change, so no rule of the code can bring the failure back.
A property for that case holds by arithmetic, and no break test can fail it, so the spec has none.

The properties read the rows of a URL through `LastRow`, `LastReads`, and `SeenAt`, and not through `Newest` and `Before`.
So a break of `Newest` or `Before` cannot also change the property that checks it.

## Findings

### 1. A transient failure in a permanent streak ends the gap (#329)

Before #329, `known_gaps()` asked that every failure of the streak is permanent (`MIN(COALESCE(permanent, 0)) = 1`).
It now counts the permanent failures (`SUM(COALESCE(permanent, 0)) >= N`).
The break `today-mixed-streak` puts that rule into the spec, and TLC stops at `LastReadsMakeGap` with this log of `live`, all at tick 0: transient, permanent, permanent.
The last two reads failed with a permanent status, and the URL is not a gap.
`GapSticky` finds the same rule from the other side: permanent, permanent is a gap, and one more transient failure ends it.

A live feed is read on every run, and a failed read of a URL that is not a gap is an error, so the refresh exits 1.
The prepare and the report both stop, because `daily-report.sh` runs `refresh --starters-only`, until the streak spans `ABANDON_MIN_SPAN_DAYS`.
This is the lost slate of #279 again, with one timeout inside a run of 403s.
The fix counts the permanent failures: at least `ABANDON_AFTER_FAILED_READS` of them make a gap.
The break `gap-from-last-reads-only` shows why a rule on the last reads alone is not enough: TLC stops at `GapSticky`.

### 2. A failure at the fetch time of a good read is not in the streak (#329)

`known_gaps()` takes the failures with `fetched_at` greater than the newest good read.
A failure that shares the fetch time of a good read, and comes after it in row order, is not counted.
The break `today-streak-by-time` puts that rule into the spec, and TLC stops at `LastReadsMakeGap` with this log of `live`, all at tick 0: ok, permanent, permanent.
`failed_since()` and `failed_urls()` already break the tie by row id (a49199c).
The production clock makes the tie rare, so #329 fixes this in the same query as finding 1.

### 3. The gap set ignores the cutoff (#330)

Before #330, `stale_sources()` and `training_gaps()` called `abandoned_urls()`, which read the whole log.
They now pass their cutoff, so the gap set reads only the reads before it.
The break `today-stale-gaps-whole-log` puts that rule into the spec, and TLC stops at `NoRowAfterCutoff`:

1. Tick 0: a read of `day` works, and a read of `old` fails.
2. Tick 1: a read of `old` fails again, so `old` is a gap from tick 1.

At the cutoff tick 1, `old` has one failure before the cutoff, which is recent and not yet a gap, so the game rows must name it.
The code sees the gap that the read at tick 1 made, and it leaves the failure out as a gap of a training season.
The break `today-footer-gaps-whole-log` shows the same fault in `training_gaps()`.

### 4. The conflict path of membership has no test

`tests/store/test_membership.py` makes at most one game for each goalie and hour (`unique_by`), so the case of two franchises at one start time never runs.
No other test calls `goalie_membership_before`.
This is a gap in the tests, not in the code, and "Goalie membership" closes it.

No other interaction of the read log or the as-of views was found wrong.

## Build order

1. #329: the gap rule, with its tests.
2. #330: the gap set as of the cutoff, with its tests.
3. This note: the spec, the configs, the shards, and the tests in "Files to add or change".

The spec states the rule after #329 and #330.
TLC checks the spec, not the code, so a spec that lands first passes and states a rule that the code does not have yet.
Do step 3 only after steps 1 and 2 are on the branch.

## Break tests

Each break edits a scratch copy of `specs/source_reads.tla`, outside the repository, and TLC must stop with the listed property.
Run each break with `-workers 1`.
With more workers, TLC searches in parallel and can name another violated property first.
"The edit of each break" gives the exact old and new text of each edit.

| Break | The code change that it stands for | Config | TLC stops with |
| --- | --- | --- | --- |
| `today-mixed-streak` | The gap rule of the code before #329 | main | `LastReadsMakeGap` |
| `today-streak-by-time` | The streak by fetch time, before #329 | main | `LastReadsMakeGap` |
| `today-stale-gaps-whole-log` | `stale_sources()` before #330 | main | `NoRowAfterCutoff` |
| `today-footer-gaps-whole-log` | `training_gaps()` before #330 | main | `NoRowAfterCutoff` |
| `gap-from-last-reads-only` | A gap from the last N reads, all permanent | main | `GapSticky` |
| `permanent-streak-waits-for-span` | The rule before #279 | main | `LastReadsMakeGap` |
| `one-permanent-failure-is-a-gap` | One permanent failure fewer than the threshold | main | `GapNeedsFailures` |
| `span-test-is-strict` | `>` in place of `>=` in the span test | main | `LastReadsMakeGap` |
| `tie-takes-first-row` | A tie at one fetch time with no row id order (a49199c) | main | `RecentFailureNamed` |
| `failures-never-reset` | A good read does not end the streak | main | `GapNeedsFailures` |
| `judged-by-newest-read-of-source` | A URL judged by the newest read of its source (#65) | main | `GoodReadClears` |
| `recent-branch-before-gap-gate` | The recent-failure test before the gap test (4880898) | main | `GapNamedOnce` |
| `live-failure-retired-like-dated` | A live failure retired after `ACTIVE_FAILURE_WINDOW` | main | `AgeStaysNamed` |
| `snapshot-has-no-age-limit` | `injury_reads_before()` with no age check (#65) | main | `SnapshotAgrees` |
| `age-test-includes-limit` | `>=` in place of `>` in the age test of `_url_problem()` | main | `SnapshotAgrees` |
| `cutoff-sees-row-at-cutoff` | `<=` in place of `<` in the cutoff filter | main | `NoRowAfterCutoff` |
| `daily-rule-reads-run-clock` | `_daily_problem()` measures from the run clock, not the cutoff | main | `AnswerFixed` |
| `reach-a-gap` | Reachability: add `Gaps(log) = {}` to `TypeOK` | main | `TypeOK` |
| `reach-a-footer-gap` | Reachability: no footer gap at any cutoff | main | `TypeOK` |
| `reach-a-retired-failure` | Reachability: no retired failure of a current-season URL | main | `TypeOK` |
| `reach-a-used-snapshot` | Reachability: no snapshot in use | main | `TypeOK` |
| `reach-a-moneypuck-age-note` | Reachability: no "old" reason for `mp` | MoneyPuck | `TypeOK` |

"main" is `specs/source_reads.cfg`, and "MoneyPuck" is `specs/source_reads_moneypuck.cfg`.
A reachability break shows that the config reaches a state that a property needs, so that property is not empty.

### The edit of each break

#### `today-mixed-streak`

Replace:

```tla
IsGap(u, L) ==
    \/ Cardinality(Streak(u, L)) >= AbandonReads /\ Spans(Streak(u, L), L)
    \/ Cardinality(Permanent(u, L)) >= AbandonReads
```

With:

```tla
IsGap(u, L) ==
    /\ Cardinality(Streak(u, L)) >= AbandonReads
    /\ Spans(Streak(u, L), L) \/ Permanent(u, L) = Streak(u, L)
```

#### `today-streak-by-time`

Replace:

```tla
Streak(u, L) == {i \in Rows(u, L) : i > LastGood(u, L)}
```

With:

```tla
Streak(u, L) ==
    {i \in Rows(u, L) :
        L[i].kind # "ok" /\ (Good(u, L) = {} \/ L[i].t > L[LastGood(u, L)].t)}
```

#### `today-stale-gaps-whole-log`

Replace:

```tla
Stale(c, L) == LET B == Before(c, L) IN StaleFrom(c, B, Gaps(B))
```

With:

```tla
Stale(c, L) == LET B == Before(c, L) IN StaleFrom(c, B, Gaps(L))
```

#### `today-footer-gaps-whole-log`

Replace:

```tla
Footer(c, L) == Gaps(Before(c, L)) \cap Training
```

With:

```tla
Footer(c, L) == Gaps(L) \cap Training
```

#### `gap-from-last-reads-only`

Replace:

```tla
    \/ Cardinality(Permanent(u, L)) >= AbandonReads
```

With:

```tla
    \/ LET R == {i \in Rows(u, L) :
                     Cardinality({j \in Rows(u, L) : j > i}) < AbandonReads}
       IN Cardinality(R) = AbandonReads /\ \A i \in R : L[i].kind = "permanent" 
```

#### `permanent-streak-waits-for-span`

Replace:

```tla
IsGap(u, L) ==
    \/ Cardinality(Streak(u, L)) >= AbandonReads /\ Spans(Streak(u, L), L)
    \/ Cardinality(Permanent(u, L)) >= AbandonReads
```

With:

```tla
IsGap(u, L) ==
    \/ Cardinality(Streak(u, L)) >= AbandonReads /\ Spans(Streak(u, L), L)
```

#### `one-permanent-failure-is-a-gap`

Replace:

```tla
    \/ Cardinality(Permanent(u, L)) >= AbandonReads
```

With:

```tla
    \/ Cardinality(Permanent(u, L)) >= AbandonReads - 1
```

#### `span-test-is-strict`

Replace:

```tla
L[Max(S)].t - L[Min(S)].t >= SpanTicks
```

With:

```tla
L[Max(S)].t - L[Min(S)].t > SpanTicks
```

#### `tie-takes-first-row`

Replace:

```tla
Newest(u, L) == Max(Rows(u, L))
```

With:

```tla
Newest(u, L) ==
    LET m == Max({L[i].t : i \in Rows(u, L)}) IN Min({i \in Rows(u, L) : L[i].t = m})
```

#### `failures-never-reset`

Replace:

```tla
Streak(u, L) == {i \in Rows(u, L) : i > LastGood(u, L)}
```

With:

```tla
Streak(u, L) == {i \in Rows(u, L) : L[i].kind # "ok"}
```

#### `judged-by-newest-read-of-source`

Replace:

```tla
    LET last == B[Newest(u, B)] IN
```

With:

```tla
    LET last == B[Max(UNION {Rows(v, B) : v \in {x \in Urls : Source(x) = Source(u)}})] IN
```

#### `recent-branch-before-gap-gate`

Replace:

```tla
            \/ u \notin gaps /\ c - last.t <= ActiveWindowTicks
```

With:

```tla
            \/ c - last.t <= ActiveWindowTicks
```

#### `live-failure-retired-like-dated`

Replace:

```tla
    THEN IF \/ u \in AgeLimited
```

With:

```tla
    THEN IF \/ u \in AgeLimited /\ c - last.t <= ActiveWindowTicks
```

#### `snapshot-has-no-age-limit`

Replace:

```tla
Live \in Urls /\ Good(Live, B) # {} /\ c - GoodAt(Live, B) <= LiveMaxAgeTicks
```

With:

```tla
Live \in Urls /\ Good(Live, B) # {}
```

#### `age-test-includes-limit`

Replace:

```tla
c - GoodAt(u, B) > Limit(u)
```

With:

```tla
c - GoodAt(u, B) >= Limit(u)
```

#### `cutoff-sees-row-at-cutoff`

Replace:

```tla
Before(c, L) == SelectSeq(L, LAMBDA r : r.t < c)
```

With:

```tla
Before(c, L) == SelectSeq(L, LAMBDA r : r.t <= c)
```

#### `daily-rule-reads-run-clock`

Replace:

```tla
c - Max(goodOf(s)) > LiveMaxAgeTicks
```

With:

```tla
now - Max(goodOf(s)) > LiveMaxAgeTicks
```

#### `reach-a-gap`

Replace:

```tla
TypeOK == log \in Seq(Row)
```

With:

```tla
TypeOK == Gaps(log) = {} /\ log \in Seq(Row)
```

#### `reach-a-footer-gap`

Replace:

```tla
TypeOK == log \in Seq(Row)
```

With:

```tla
TypeOK == (\A c \in Cutoffs : Footer(c, log) = {}) /\ log \in Seq(Row)
```

#### `reach-a-retired-failure`

Replace:

```tla
TypeOK == log \in Seq(Row)
```

With:

```tla
TypeOK ==
    /\ ~\E c \in Cutoffs, u \in Urls \ AgeLimited :
          LET B == Before(c, log) IN
          Rows(u, B) # {} /\ B[Max(Rows(u, B))].kind # "ok" /\ ~Named(u, c, log)
          /\ ~NamedNever(u, c, log) /\ u \notin Training
    /\ log \in Seq(Row)
```

#### `reach-a-moneypuck-age-note`

Replace:

```tla
TypeOK == log \in Seq(Row)
```

With:

```tla
TypeOK ==
    /\ \A c \in Cutoffs : [at |-> Mp, why |-> "old"] \notin Stale(c, log)
    /\ log \in Seq(Row)
```

#### `reach-a-used-snapshot`

Replace:

```tla
TypeOK == log \in Seq(Row)
```

With:

```tla
TypeOK == (\A c \in Cutoffs : ~SnapshotUsed(c, log)) /\ log \in Seq(Row)
```

## Goalie membership

Extend `tests/store/test_membership.py`.
Keep the existing test and its oracle, and change the strategy and the assertions as below.
Use `hypothesis`, the `seeding` helpers (`add_game`, `add_goalie`, `utc`, `FETCHED`), and a temporary store for each example, as the test does today.

### The strategy

1. `TEAMS = ["TOR", "MTL", "BOS", "ARI", "UTA"]`. `ARI` and `UTA` are one franchise (`franchise("ARI") == "UTA"`), so a code change is in the data.
2. An appearance stays `(goalie id, team, hour)`, with the goalie id from 1 to 3 and the hour from 0 to `40 * 24`.
3. Change `unique_by` to `lambda a: (a[0], a[1], a[2])`.
   Two appearances of one goalie at one hour for two teams are then possible, which is the conflict case.
   Each appearance stays its own game, with its own game number, as today.
4. Keep `max_size=10`, and raise `max_examples` to 50.
5. Add two `@example` cases for the cutoff boundary, because random hours seldom land on it: goalie 1 for `TOR` at hour 700 and for `MTL` at `LAST_HOUR`, where the `MTL` game counts; and goalie 1 for `TOR` at hour 700 and for `MTL` at `LAST_HOUR + 1`, where the `MTL` game does not count.

### The oracle

For each goalie, take the appearances with `hour <= LAST_HOUR`.
With none, the goalie is in neither set.
Otherwise take the newest such hour, and the set of franchises of the appearances at that hour.
More than one franchise means conflicting.
One franchise means departed for every asking team of another franchise, and neither set for an asking team of that franchise.

### The assertions

| Name | Assertion |
| --- | --- |
| M1, the newest game decides | For each asking team in `TEAMS`, the result equals the oracle. |
| M2, one home | For a goalie with a counted game and no conflict, the franchises of `TEAMS` that do not call the goalie departed are exactly one: the franchise of the newest game. |
| M3, a conflict is the same for every team | A conflicting goalie is conflicting for every asking team, and departed for none. |
| M4, a later game does not count | Add one more game at an hour after `LAST_HOUR`, for any goalie and team that Hypothesis draws, to the same store. The result for every asking team does not change. |
| M5, the fetch time does not count | Write the same appearances to a second store with the `fetched_at` values in reverse order. The result for every asking team is the same. |
| M6, a code change is not a departure | A goalie whose newest counted game is for `ARI` is not departed for `UTA`, and one whose newest game is for `UTA` is not departed for `ARI`. |

M2 and M3 are the meaning of "the same whoever asks".
M1 already covers the trade back, because the newest game decides and an older game for another franchise does not.
M6 follows from M1 with `TEAMS` above, but keep it as its own assertion, because its failure message names the code change.

### Break tests

Make each edit in `src/hockey_model/store/membership.py`, run `uv run pytest tests/store/test_membership.py`, confirm that the listed assertion fails, and revert the edit.
Record the result in the pull request.
These breaks were not run when this note was written.

| Break | Edit | Fails |
| --- | --- | --- |
| No conflict check | Delete the `if len(teams) > 1:` branch, so the `elif` becomes `if` | M3 |
| No result delay | `cutoff = iso_utc(before)` in place of `result_cutoff(before)` | M1 |
| No trade back | Departed when any game before the cutoff is for another franchise | M1 and M2 |
| Codes, not franchises | `_newest_teams` returns `{team for (team,) in cursor}`, and `home = team` | M1 and M6 |
| No cutoff | Drop `AND g2.start_utc <= ?` from the inner select, and its parameter | M1 and M4 |

## Files to add or change

1. `specs/source_reads.tla`: copy the reference spec from the appendix without change.
2. `specs/source_reads.cfg` and `specs/source_reads_moneypuck.cfg`: copy the reference configs from the appendix without change.
3. `specs/shards.json`: add the two shards below, after the `registry` shard and before the `stall` shard, so the small shards run first.
4. `tests/test_tla_source_reads.py`: the constants test and the structure tests below.
5. `tests/test_tla_appendix.py`: check this note as well as `daily-prepare-tla.md`. Replace `NOTE` and `FILES` with a map from each note to its files, and parametrize the test over each pair of note and file.
6. `tests/store/test_membership.py`: the test in "Goalie membership".
7. `docs/design/tla-scope.md`: change the result of the read-log row to "Passes. Modelled in `specs/source_reads.tla` (#264). Goalie membership is a pure function, and a property test covers it."
8. `.github/workflows/tla.yml`: change the first line of the header comment to "TLC checks the spec of each shard in specs/shards.json." The workflow itself does not change.
9. `AGENTS.md`: add `specs/source_reads.tla` to the list of specs in the "Model check" line.
10. `CHANGELOG.md`: one `Added` entry for the model of the read log and the as-of views.

The daily report does not change.
The spec is not an input source, a model, or a feature of the report, so the report rule of `AGENTS.md` does not apply.
`.editorconfig` already has a `[*.{tla,cfg}]` section.
`scripts/tlc.sh` reads its shard list from `specs/shards.json`, so its usage text lists the new shards with no change of the script.

### The shards

```json
    {
      "name": "reads-mp",
      "spec": "specs/source_reads.tla",
      "config": "specs/source_reads_moneypuck.cfg",
      "covers": [
        "src/hockey_model/store/source_reads.py",
        "src/hockey_model/store/staleness.py",
        "src/hockey_model/store/reads.py",
        "tests/test_tla_source_reads.py"
      ]
    },
    {
      "name": "reads",
      "spec": "specs/source_reads.tla",
      "config": "specs/source_reads.cfg",
      "covers": [
        "src/hockey_model/store/source_reads.py",
        "src/hockey_model/store/staleness.py",
        "src/hockey_model/store/reads.py",
        "tests/test_tla_source_reads.py"
      ]
    },
```

The `covers` list names the modules that own a rule of "What the code does", and the constants test that ties the configs to the code.
The model takes `PERMANENT_STATUSES`, the season of a URL, and the live-feed read of `store/refresh_other.py` as given, so those modules are not listed.
`store/membership.py` is not listed, because the Hypothesis test covers it.
A change of any listed path, of the spec, or of a config starts both shards.

### `tests/test_tla_source_reads.py`

Follow `tests/test_tla_store_upgrade.py`, and import `parse_constants` from `test_tla_constants`.

1. `CFGS`: the configs of every shard in `specs/shards.json` whose `spec` is `specs/source_reads.tla`. Fail when there are fewer than two.
2. `TICK = timedelta(hours=12)`, and one function `ticks(span, name)` that returns `span // TICK`. It fails, and names the constant, the span, and the tick, when `span % TICK` is not zero. The message says to change `TICK` and the tick values of every `source_reads` config together.
3. `EXPECTED`: `AbandonReads` is `ABANDON_AFTER_FAILED_READS`. `SpanTicks` is `ticks(timedelta(days=ABANDON_MIN_SPAN_DAYS), ...)`. `LiveMaxAgeTicks`, `ActiveWindowTicks`, and `MoneyPuckMaxAgeTicks` are `ticks()` of `LIVE_FEED_MAX_AGE`, `ACTIVE_FAILURE_WINDOW`, and `MONEYPUCK_MAX_AGE`.
4. For each config and each name of `EXPECTED`: the config holds the name, the value is an integer, and it equals the expected value. A failure names the config and the constant.
5. For each config: `MaxCutoff >= ActiveWindowTicks + MaxTime + 1` when `Urls` holds `"day"` or `"old"`, and `MaxCutoff >= MoneyPuckMaxAgeTicks + MaxTime + 1` when it holds `"mp"`.
6. The `Urls` of all configs together are `{"live", "day", "old", "mp"}`.
7. For each config: every name under `INVARIANTS` and `PROPERTIES` is defined in the spec, and the config checks every zero-argument definition of the spec except these helpers: `Live`, `Day`, `Old`, `Mp`, `AllUrls`, `Sources`, `AgeLimited`, `DailySources`, `Training`, `Cutoffs`, `Row`, `Init`, `Tick`, `Next`, `Spec`.
8. `ticks(timedelta(hours=18), "X")` fails with a message that names `X`. This is the edge case of item 2.

## Verification

1. `bash scripts/tlc.sh --shard reads-mp` and `bash scripts/tlc.sh --shard reads` pass, with the state counts in "Constants".
   `bash scripts/tlc.sh` runs every shard and passes.
2. Each break test fails as listed, with `-workers 1`.
   Run them on a scratch copy outside the repository, and do not commit the broken copies.
3. Each membership break fails as listed, and the edit is reverted.
4. `uv run pytest`, `uv run python3 scripts/check_per_module_coverage.py`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run ty check` pass.
5. `prek run --all-files` passes.
6. The `tla (reads-mp)` and `tla (reads)` jobs pass on the pull request. Report the time of the `reads` job in the pull request.

## Keep the spec in step

The mapping from the spec to the code stays manual.
A change to a rule in "What the code does" MUST update the spec, its break tests, and this note in the same pull request.
The two shards start TLC on such a change, and a new module that owns one of these rules goes into their `covers` lists in the same pull request.
Issue #336 plans a fingerprint lock that flags a changed rule before CI.
Until #336 lands, this section is the only guard against drift.

## Not modeled

- The HTTP reads, which `sources/http.py` and its tests cover.
- The backfill windows and the exit codes, which `specs/daily_prepare.tla` models.
- Clock skew between runners. A row with a fetch time before the fetch time of an earlier row breaks the prefix rule of the model. `FUTURE_READ_TOLERANCE` in `live_gaps.py` deals with a skewed read on the alert side only.
- Two URLs of one single feed. `_url_problem()` judges each URL, so an old URL of a single feed, with a newest good read older than the limit, is named at every later cutoff. The code has one URL for each single feed, so this happens only when a URL changes.
- Two current MoneyPuck files, and the reason for a file with no read.
- `espn_starters` and `daily_faceoff`. Their rules are the rules of `nhl_scores` (a daily source) and of `espn_injuries` (a single feed).
- The season of a cutoff. `_changes_prediction()` depends on the season of the cutoff, and the model gives each dated URL one fixed scope.
- An expected absence (`reading.expected_absence`), which writes no `source_reads` row and is therefore not a read.

## Appendix: reference spec

TLC 1.7.4 checked this spec with both configs, and found no violation.
Copy all three files byte for byte.
`tests/test_tla_appendix.py` checks that each file and its copy here are equal.

### `specs/source_reads.tla`

```tla
------------------------------ MODULE source_reads ------------------------------
\* A model of the read log (src/hockey_model/store/source_reads.py) and of the as-of views that
\* read it (store/staleness.py and the injury snapshot of store/reads.py). A run appends one row
\* for each read, and the clock moves forward. The properties ask about every cutoff, so the
\* cutoff is not a variable. The mapping is in docs/design/source-reads-tla.md.
\*
\* The model states the rule of the code after #329 and #330.

EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    AbandonReads,          \* ABANDON_AFTER_FAILED_READS
    SpanTicks,             \* ABANDON_MIN_SPAN_DAYS, in ticks
    LiveMaxAgeTicks,       \* LIVE_FEED_MAX_AGE, in ticks
    ActiveWindowTicks,     \* ACTIVE_FAILURE_WINDOW, in ticks
    MoneyPuckMaxAgeTicks,  \* MONEYPUCK_MAX_AGE, in ticks
    Urls,                  \* the URLs of this config, a subset of AllUrls
    PermanentUrls,         \* the URLs that can fail with a permanent status
    MaxReads,              \* the most rows in the log
    MaxTime,               \* the last tick at which a read can happen
    MaxCutoff              \* the last cutoff that the properties ask about

\* One URL of each kind that the as-of rules tell apart.
Live == "live"  \* the one URL of a single live feed, such as espn_injuries
Day == "day"    \* a score day of a season that can move a prediction
Old == "old"    \* a score day of an old training season: its gap goes in the footer
Mp == "mp"      \* a current MoneyPuck file
AllUrls == {Live, Day, Old, Mp}

ASSUME Urls \subseteq AllUrls /\ Urls # {} /\ PermanentUrls \subseteq Urls
ASSUME AbandonReads \in Nat /\ AbandonReads >= 1
ASSUME {SpanTicks, LiveMaxAgeTicks, ActiveWindowTicks, MoneyPuckMaxAgeTicks} \subseteq Nat
ASSUME MaxCutoff >= MaxTime + 1

Source(u) == CASE u = Live -> "espn_injuries"
             []   u = Mp -> "moneypuck"
             []   OTHER -> "nhl_scores"
Sources == {Source(u) : u \in Urls}
\* _SINGLE_FEEDS and _age_limit: the URLs whose newest good read must be recent.
AgeLimited == {Live, Mp} \cap Urls
Limit(u) == IF u = Mp THEN MoneyPuckMaxAgeTicks ELSE LiveMaxAgeTicks
\* _DAILY_FEEDS: at least one URL of the source must have a recent good read.
DailySources == {"nhl_scores"} \cap Sources
\* _changes_prediction is FALSE for a dated URL of an old training season only.
Training == {Old} \cap Urls

Kinds(u) == {"ok", "transient"} \cup (IF u \in PermanentUrls THEN {"permanent"} ELSE {})
Cutoffs == 0 .. MaxCutoff

VARIABLES log, now
vars == <<log, now>>

Row == [url : Urls, t : 0 .. MaxTime, kind : {"ok", "transient", "permanent"}]

Max(S) == CHOOSE x \in S : \A y \in S : y <= x
Min(S) == CHOOSE x \in S : \A y \in S : x <= y

\* ---------------------------------------------------------------------------------------------
\* The read log. The clock never runs back, so the row order is the (fetched_at, rowid) order
\* of the SQL, and two rows can share one tick.

Rows(u, L) == {i \in 1 .. Len(L) : L[i].url = u}
Good(u, L) == {i \in Rows(u, L) : L[i].kind = "ok"}
\* Only for a URL with a row.
Newest(u, L) == Max(Rows(u, L))
LastGood(u, L) == IF Good(u, L) = {} THEN 0 ELSE Max(Good(u, L))
\* The failed reads after the newest good read.
Streak(u, L) == {i \in Rows(u, L) : i > LastGood(u, L)}
Permanent(u, L) == {i \in Streak(u, L) : L[i].kind = "permanent"}
\* Only for a set with a row.
Spans(S, L) == L[Max(S)].t - L[Min(S)].t >= SpanTicks

\* known_gaps: enough failures that span the minimum, or enough permanent failures.
IsGap(u, L) ==
    \/ Cardinality(Streak(u, L)) >= AbandonReads /\ Spans(Streak(u, L), L)
    \/ Cardinality(Permanent(u, L)) >= AbandonReads
Gaps(L) == {u \in Urls : IsGap(u, L)}

\* ---------------------------------------------------------------------------------------------
\* The as-of views. A cutoff sees the rows fetched before it. Because the clock never runs
\* back, these rows are a prefix of the log.

Before(c, L) == SelectSeq(L, LAMBDA r : r.t < c)
\* Only for a URL with a good read.
GoodAt(u, B) == B[LastGood(u, B)].t

\* _url_problem: the reason that one URL gives, as a set of zero or one word.
UrlReasons(u, c, B, gaps) ==
    LET last == B[Newest(u, B)] IN
    IF last.kind # "ok"
    THEN IF \/ u \in AgeLimited
            \/ u \in gaps /\ u \notin Training
            \/ u \notin gaps /\ c - last.t <= ActiveWindowTicks
         THEN {"failed"}
         ELSE {}
    ELSE IF u \in AgeLimited /\ c - GoodAt(u, B) > Limit(u) THEN {"old"} ELSE {}

\* stale_sources, from the rows B that the cutoff c sees and the gaps of those rows.
StaleFrom(c, B, gaps) ==
    LET never == {s \in Sources : \A u \in Urls : Source(u) = s => Good(u, B) = {}}
        seen == {u \in Urls : Rows(u, B) # {} /\ Source(u) \notin never}
        goodOf(s) == {GoodAt(u, B) : u \in {v \in Urls : Source(v) = s /\ Good(v, B) # {}}}
        daily == {s \in DailySources \ never : c - Max(goodOf(s)) > LiveMaxAgeTicks}
    IN  {[at |-> s, why |-> "never"] : s \in never}
        \cup UNION {{[at |-> u, why |-> w] : w \in UrlReasons(u, c, B, gaps)} : u \in seen}
        \cup {[at |-> s, why |-> "daily"] : s \in daily}

Stale(c, L) == LET B == Before(c, L) IN StaleFrom(c, B, Gaps(B))
\* training_gaps: the gaps of an old training season, listed once in the report footer.
Footer(c, L) == Gaps(Before(c, L)) \cap Training
Named(u, c, L) == \E r \in Stale(c, L) : r.at = u
NamedNever(u, c, L) == [at |-> Source(u), why |-> "never"] \in Stale(c, L)

\* injury_reads_before: the newest good read of the injury list is used while it is recent.
SnapshotUsed(c, L) ==
    LET B == Before(c, L) IN
    Live \in Urls /\ Good(Live, B) # {} /\ c - GoodAt(Live, B) <= LiveMaxAgeTicks

\* ---------------------------------------------------------------------------------------------
\* The runs. A read of a dated gap stands for `refresh --retry-skipped`. A read of a live gap is
\* the read that every run makes (#276).

Init == log = <<>> /\ now = 0

Read(u, k) ==
    /\ Len(log) < MaxReads
    /\ log' = Append(log, [url |-> u, t |-> now, kind |-> k])
    /\ UNCHANGED now

Tick == now < MaxTime /\ now' = now + 1 /\ UNCHANGED log

Next == (\E u \in Urls : \E k \in Kinds(u) : Read(u, k)) \/ Tick

Spec == Init /\ [][Next]_vars

\* ---------------------------------------------------------------------------------------------
\* Invariants. They read the rows of a URL in row order through LastRow and LastReads, and not
\* through Newest, so that a change of Newest cannot also change the property that checks it.

TypeOK == log \in Seq(Row) /\ Len(log) <= MaxReads /\ now \in 0 .. MaxTime

\* The last row of a URL. Only for a URL with a row.
LastRow(u, L) == L[Max(Rows(u, L))]

\* The newest AbandonReads rows of a URL, in row order.
LastReads(u, L) ==
    {i \in Rows(u, L) : Cardinality({j \in Rows(u, L) : j > i}) < AbandonReads}

\* A gap needs AbandonReads failed reads after the newest good read.
GapNeedsFailures ==
    \A u \in Gaps(log) :
        /\ Cardinality(LastReads(u, log)) = AbandonReads
        /\ \A i \in LastReads(u, log) : log[i].kind # "ok"

\* The rule that the issue states: the last AbandonReads reads failed, and they span the
\* minimum or every one of them is permanent. Such a URL is a gap.
LastReadsMakeGap ==
    \A u \in Urls :
        LET R == LastReads(u, log) IN
        ( /\ Cardinality(R) = AbandonReads
          /\ \A i \in R : log[i].kind # "ok"
          /\ \/ log[Max(R)].t - log[Min(R)].t >= SpanTicks
             \/ \A i \in R : log[i].kind = "permanent" )
        => u \in Gaps(log)

\* The last read of a URL worked: it is no gap, and no later cutoff names it as failed.
GoodReadClears ==
    \A u \in Urls :
        (Rows(u, log) # {} /\ LastRow(u, log).kind = "ok") =>
            /\ u \notin Gaps(log)
            /\ \A c \in Cutoffs :
                c > LastRow(u, log).t => [at |-> u, why |-> "failed"] \notin Stale(c, log)

\* The rows that a cutoff may see. The properties keep this copy of the rule of Before, so that a
\* change of Before shows as a violation.
SeenAt(c, L) == SelectSeq(L, LAMBDA r : r.t < c)

\* No answer at a cutoff depends on a row fetched at or after it.
NoRowAfterCutoff ==
    \A c \in Cutoffs :
        LET B == SeenAt(c, log) IN
        /\ Stale(c, log) = Stale(c, B)
        /\ Footer(c, log) = Footer(c, B)
        /\ SnapshotUsed(c, log) = SnapshotUsed(c, B)

\* A failure inside the active window is named, unless the URL is a gap of a training season.
RecentFailureNamed ==
    \A c \in Cutoffs, u \in Urls :
        LET B == Before(c, log) IN
        ( /\ Rows(u, B) # {}
          /\ LastRow(u, B).kind # "ok"
          /\ c - LastRow(u, B).t <= ActiveWindowTicks
          /\ ~(u \in Training /\ u \in Gaps(B)) )
        => (Named(u, c, log) \/ NamedNever(u, c, log))

\* A gap of a dated URL is named once: in the footer for a training season, else on the rows.
GapNamedOnce ==
    \A c \in Cutoffs, u \in Urls \ AgeLimited :
        u \in Gaps(Before(c, log)) =>
            /\ (u \in Footer(c, log)) = (u \in Training)
            /\ u \in Training => ~Named(u, c, log)
            /\ u \notin Training => (Named(u, c, log) \/ NamedNever(u, c, log))

\* A URL with an age limit that is named at one cutoff is named at the next, while no new
\* read of it arrives.
AgeStaysNamed ==
    \A c \in 0 .. MaxCutoff - 1, u \in AgeLimited :
        (Named(u, c, log) /\ Rows(u, Before(c, log)) = Rows(u, Before(c + 1, log)))
        => Named(u, c + 1, log)

\* The injury snapshot and the stale note agree: a snapshot in use is not too old, and a
\* snapshot that is not used is named.
SnapshotAgrees ==
    \A c \in Cutoffs :
        Live \in Urls =>
            /\ SnapshotUsed(c, log) => [at |-> Live, why |-> "old"] \notin Stale(c, log)
            /\ (Good(Live, Before(c, log)) # {} /\ ~SnapshotUsed(c, log)) => Named(Live, c, log)

\* ---------------------------------------------------------------------------------------------
\* Action properties.

GoodAppended(u) ==
    Len(log') > Len(log) /\ log'[Len(log')].url = u /\ log'[Len(log')].kind = "ok"

\* A gap stays a gap until a good read of that URL.
GapSticky == [][\A u \in Urls : (u \in Gaps(log) /\ ~GoodAppended(u)) => u \in Gaps(log')]_vars

\* Once the clock is past a cutoff, the answer at that cutoff never changes: a later read, or a
\* later tick, cannot bring a retired failure back or hide a named one.
AnswerFixed ==
    [][\A c \in 0 .. now :
          /\ Stale(c, log)' = Stale(c, log)
          /\ Footer(c, log)' = Footer(c, log)
          /\ SnapshotUsed(c, log)' = SnapshotUsed(c, log)]_vars

==================================================================================================
```

### `specs/source_reads_moneypuck.cfg`

```text
SPECIFICATION Spec
CHECK_DEADLOCK FALSE

CONSTANTS
    AbandonReads = 2
    SpanTicks = 1
    LiveMaxAgeTicks = 2
    ActiveWindowTicks = 14
    MoneyPuckMaxAgeTicks = 28
    Urls = {"live", "mp"}
    PermanentUrls = {}
    MaxReads = 3
    MaxTime = 3
    MaxCutoff = 33

INVARIANTS
    TypeOK
    GapNeedsFailures
    LastReadsMakeGap
    GoodReadClears
    NoRowAfterCutoff
    RecentFailureNamed
    GapNamedOnce
    AgeStaysNamed
    SnapshotAgrees

PROPERTIES
    GapSticky
    AnswerFixed
```

### `specs/source_reads.cfg`

```text
SPECIFICATION Spec
CHECK_DEADLOCK FALSE

CONSTANTS
    AbandonReads = 2
    SpanTicks = 1
    LiveMaxAgeTicks = 2
    ActiveWindowTicks = 14
    MoneyPuckMaxAgeTicks = 28
    Urls = {"live", "day", "old"}
    PermanentUrls = {"live"}
    MaxReads = 4
    MaxTime = 3
    MaxCutoff = 18

INVARIANTS
    TypeOK
    GapNeedsFailures
    LastReadsMakeGap
    GoodReadClears
    NoRowAfterCutoff
    RecentFailureNamed
    GapNamedOnce
    AgeStaysNamed
    SnapshotAgrees

PROPERTIES
    GapSticky
    AnswerFixed
```
