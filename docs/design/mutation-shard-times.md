# Mutation shard times (#293, #323)

## Problem

The weekly `mutation.yml` run did not finish.
After the stats phase was fixed, every shard hit the 25-minute step limit (run 45938682648).
The shard table in `scripts/mutation_shards.py` had been sized from estimates and from an older run.

## Measurement

Two runs recorded the work of every mutant.
Run A (45941517476) ran the old table of four shards with a 150-minute step limit.
Run B (45949049995) ran a table of five shards sized from run A, with a 60-minute step limit.
The work of a module is the sum of `durations_by_key` over its mutants in the `.meta` files.

| Module | Mutants | Run A (s) | Run B (s) | Mean (s) |
| --- | --- | --- | --- | --- |
| game_model.logistic | 369 | 5833 | 8915 | 7374 |
| experiments.models | 126 | 7647 | 3855 | 5751 |
| backtest.runner | 485 | 2922 | 5256 | 4089 |
| backtest.metrics | 211 | 4453 | 3251 | 3852 |
| backtest.row_cache | 128 | 3730 | 3720 | 3725 |
| backtest.row_codec | 420 | 3893 | 2807 | 3350 |
| sources.espn | 552 | 1463 | 2818 | 2141 |
| sources.nhl | 707 | 1519 | 2532 | 2026 |
| backtest.season_key | 134 | 2182 | 1588 | 1885 |
| json_schema | 113 | 2140 | 1519 | 1830 |
| sources.daily_faceoff | 216 | 716 | 1036 | 876 |
| backtest.shards | 186 | 556 | 998 | 777 |
| backtest.units | 124 | 537 | 908 | 723 |
| sources.moneypuck | 326 | 430 | 716 | 573 |
| sources.payload | 84 | 324 | 550 | 437 |
| game_model.elo | 78 | 470 | 479 | 475 |
| backtest.golden | 124 | 238 | 195 | 217 |
| sources.http | 204 | 157 | 173 | 165 |
| sources.teams, sources.errors, sources.shape_errors, sources.sanitize | 58 | 10 | 17 | 14 |

The total is 39,221 s in run A and 41,333 s in run B.
`sources.policy` and `sources.starters` hold no function, so mutmut makes no mutant for them.
They stay in shard 4, because the partition check needs every module that `only_mutate` names.

## The speed of the runner changes from run to run

The work of one module differs by up to a factor of two between the runs: `experiments.models` took 7647 s in run A and 3855 s in run B, and `game_model.logistic` took 5833 s and 8915 s.
The cause is the runner, and not the module.
The mutant generation step of mutmut (`done in ... ms` in the log of each shard) does the same work in every shard.
It took this long:

| Run | Shard 0 | Shard 1 | Shard 2 | Shard 3 | Shard 4 |
| --- | --- | --- | --- | --- | --- |
| A, 45941517476 (ms) | 5798 | 5841 | 3311 | 3270 | |
| B, 45949049995 (ms) | 3015 | 5216 | 4119 | 4236 | 5983 |

So a runner can be twice as slow as another one.
`experiments.models` ran on a slow runner in run A and on the fastest runner in run B, and `game_model.logistic` ran the other way.
The work of the large modules agrees across the two runs to within 4 % when each module is scaled to the fastest runner (generation time of the fastest runner divided by the generation time of its shard).
The small modules differ more, because per-mutant startup dominates them.

The shard table of run B was sized from run A, so its shards took 22, 35, 39, 56, and 60 minutes, not the 40 minutes that the fit predicted.
Shard 1 of run B finished with less than one minute to spare under the 60-minute limit.
The speed of a runner applies to a whole shard at once, so the step limit has to cover a slow runner.
The table uses the mean of the two runs, which only works because the two runs put `experiments.models` and `game_model.logistic` on opposite runners.

## Wall time of a shard

For the same run, the wall time of a shard follows `wall = 240 s + work / 3.6`: 240 s of setup and stats phase, and 3.6 effective workers.
Run B gives 22.8, 55.2, and 60.5 minutes for the three shards that the fit checks, against 22, 55.5, and 59.8 minutes measured.

## Choice

No split of four shards fits a 25-minute limit. A shard of 23 minutes holds about 4,100 s of work, and the total needs ten such shards.

The table has six shards. Scaled to the fastest runner, a shard holds 3,800 to 5,300 s of work.
The fit predicts 22 to 28 minutes on the fastest runner and 39 to 53 minutes on a runner that is twice as slow.
The slowest case is `game_model.logistic` alone, at about 53 minutes.
The scheduled step limit is 75 minutes, so that case has a margin of 22 minutes.
The weekly run is advisory, and no one waits for it, so a longer limit costs runner time and nothing else.

## Resize the table

1. Run `mutation.yml` by hand with `step_minutes` set high enough for every shard to finish (see `scripts/mutation-limits.sh`).
2. Read the work of each module from the `.meta` files, and the generation step time of each shard from its log.
3. Scale the work of each module by the generation time of the fastest shard divided by the generation time of its own shard.
4. Pack the scaled work into shards, and set the step limit for a runner that is twice as slow as the fastest one.

One run is enough when the work is scaled this way.

## Not done here

A split by function (#176) is the way to shorter shards. It needs the `Class.method` entries and the rest entry in `docs/design/mutation-function-shards.md`.
Record the generation step time of each shard next to its durations, so that the scaling reads data and not a log.

## Modules added after the measurement

`sources.nhl_events`, `sources.nhl_shifts`, `sources.espn_summary`, and `stints` (#35) are not in the table above, because no run has measured them.
They sit in shards 2 and 3, beside modules of the same kind.
The next measured run sizes them. The step limit leaves room for the added work until then.
