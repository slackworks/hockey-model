# Mutation shard times (#293, #323)

## Problem

The weekly `mutation.yml` run did not finish.
After the stats phase was fixed, every shard hit the 25-minute step limit (run 45938682648).
The shard table in `scripts/mutation_shards.py` had been sized from estimates and from an older run.

## Measurement

Run 45941517476 ran the table of four shards with a 150-minute step limit, so every shard finished.
The work of a module is the sum of `durations_by_key` over its mutants in the `.meta` files.

| Module | Mutants | Work (s) |
| --- | --- | --- |
| experiments.models | 126 | 7647 |
| game_model.logistic | 369 | 5833 |
| backtest.metrics | 211 | 4453 |
| backtest.row_codec | 420 | 3893 |
| backtest.row_cache | 128 | 3730 |
| backtest.runner | 485 | 2922 |
| backtest.season_key | 134 | 2182 |
| json_schema | 113 | 2140 |
| sources.nhl | 707 | 1519 |
| sources.espn | 552 | 1463 |
| sources.daily_faceoff | 216 | 716 |
| backtest.shards | 186 | 556 |
| backtest.units | 124 | 537 |
| game_model.elo | 78 | 470 |
| sources.moneypuck | 326 | 430 |
| sources.payload | 84 | 324 |
| backtest.golden | 124 | 238 |
| sources.http | 204 | 157 |
| sources.teams, sources.errors, sources.shape_errors, sources.sanitize | 58 | 10 |

The total is 39,221 s of mutant work.
`sources.policy` and `sources.starters` hold no function, so mutmut makes no mutant for them.
They stay in shard 1, because the partition check needs every module that `only_mutate` names.

## Wall time of a shard

The four shards of that run took 50.2, 70.1, 18.4, and 58.5 minutes for 9787, 14258, 2774, and 12401 s of work.
A fit gives `wall = 240 s + work / 3.6`: 240 s of setup and stats phase, and 3.6 effective workers.
It predicts the three slow shards within 3 minutes.

## Choice

No split of four shards fits a 25-minute limit. A shard of 23 minutes holds about 4100 s of work, and the total needs ten such shards.
`experiments.models` alone is 7647 s, which is 39.4 minutes. A split by module cannot go below that floor.

The table now has five shards of 7800 to 7900 s each, which the fit predicts at 40 to 41 minutes.
The scheduled step limit is 60 minutes, so a shard has a margin of about 19 minutes.
The weekly run is advisory, and no one waits for it, so a longer shard costs runner time and nothing else.

## Not done here

A split by function (#176) is the way below 39 minutes. It needs the `Class.method` entries and the rest entry in `docs/design/mutation-function-shards.md`.
Resize the table from a new CI run when a module joins `only_mutate` or its tests change much.
A manual run with `step_minutes` set (see `scripts/mutation-limits.sh`) gives the durations of a table that does not fit.
