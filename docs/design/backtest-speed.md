# Backtest speed (#138)

## Goal

Make `hockey-model backtest` and `hockey-model experiments run` faster with no change in any output.
The canonical hashes of the JSON report and the per-game CSV gate every change in the tests. The exact hashes of the two files MUST match on one machine before and after a change.

## Step 1: the golden check

Build this before any speed change, in its own pull request.

1. `scripts/backtest_golden.py --db PATH --out DIR` runs the backtest and writes `report.json`, `games.csv`, and `hashes.json`. `hashes.json` holds the exact SHA-256 of the two files (`exact_report_sha256`, `exact_games_sha256`), which two runs on one machine must match, and a canonical form with every float rounded to nine decimals (`canonical_*`), which also holds across machines.
2. `tests/backtest/test_golden.py` runs the same backtest on the seeded store and compares the canonical hashes with the values committed in `tests/fixtures/backtest_golden.json`, for one worker and for two. The fixture also holds the hash of the fitted weights of the seeded store (`canonical_fit_sha256`), so a change to the fit, such as #289, is gated too. A change that moves a hash fails the test.
3. A change that is meant to move a number is a model change. It runs the test with `UPDATE_GOLDEN=1`, updates the golden file in a separate commit, and the pull request says why.

Run the script on the real store (`~/.cache/hockey-model/real.db`) before and after each speed change too, and paste the two hashes in the pull request.
The seeded store is small, so a real-store check catches an order or rounding change that the seed does not reach.

## Step 2: the profile

Run `uv run python3 -m cProfile -o .tmp/backtest.prof -m hockey_model.cli backtest` on the real store.
Print the 30 most expensive functions by cumulative time with `pstats`.
Write the table in the issue, in these groups: feature rows (SQL and Python), Elo replay, the logistic fits, the validation fits, the bootstrap, and the CSV write.
Also time one `experiments run 0002-v1-forward-test` on the same store.

## Step 3: candidates

Try them in this order, and keep each one only when the golden check passes.
Write the verdict and the measured saving of each in the issue.

1. One snapshot read. Load the tables that `build_features` reads into memory once per run: final games, team MoneyPuck rows, goalie MoneyPuck rows, goalie starts, starter reads. Index each by team or player and sorted by start time. Replace each `*_before` SQL read with `bisect` on the sorted list. Keep the SQL readers for the live prediction path, and add a test that the two readers return equal rows for 200 random `(team, cutoff)` pairs of the seeded store.
2. Share the actual and the guess rows. `run_backtest` builds `training_rows` twice. Only the goalie values differ. Build the Elo logits, the team forms, and the rest once, and the goalie values once for each mode.
3. Share the catalog and the Elo replay between the two builds (part of candidate 2).
4. The bootstrap with `numpy`. `block_bootstrap_mean_ci` draws `BOOTSTRAP_RESAMPLES` block starts. A NumPy version gives the same numbers only when it draws the same block starts in the same order. Keep the current `random.Random(BOOTSTRAP_SEED)` draws, and move only the sums to NumPy. If the sums change in the last bit, the candidate fails the golden check, and it is rejected.
5. Parallel build of the actual and the guess rows with `concurrent.futures.ProcessPoolExecutor`. Each process opens its own read-only connection. Keep it only when it saves more than 20% on the CI runner, which has 4 cores.
6. SQLite: open the backtest connection with `PRAGMA query_only = ON` and `PRAGMA cache_size = -262144` (256 MiB). Add an index only when the profile shows a full scan.
7. A disk cache of feature rows, keyed by the store fingerprint and the code commit. Defer it: it adds a cache invalidation rule for a gain that candidates 1 to 3 likely give already. File an issue when the profile shows that feature rows still cost most of the time after candidate 3.

## Step 4: Actions

1. The `daily-prepare` job already caches the store. Measure the `uv` cache hit and the store restore time from the job notices (#154 covers the job steps).
2. A matrix split by scored season does not work: each scored season needs the fit of all earlier seasons, and the validation fits are shared. Reject it with that reason.
3. Larger runners cost money on a public repository. Reject them unless the owner asks.
4. Running mutation on changed files only is #76. Link it, do not repeat it.

## Library use

The research harness adds `numpy` (H1 in [research-harness.md](research-harness.md)).
This issue MAY use it for candidate 4 only.
It MUST NOT replace `game_model/logistic.py` with a library fit, because a library solver gives different last bits and so a different report.
That replacement is a model change and needs its own issue.

## Done when

- The profile, each candidate's verdict, and the time before and after are in the issue.
- The golden test passes on the seeded store, and the real-store hashes match before and after.
- The full backtest time on the real store is lower by a stated amount.

## Calls made

- The golden hash check comes first, so no speed change can merge without proof of the same output.
- The NumPy bootstrap keeps the standard-library draws, so the block starts stay the same.

## Parallel units (#283)

The backtest is a set of named units.
Each unit logs `backtest: unit NAME took Ns` on stderr, and the run ends with `backtest: total Ns`.
`backtest --workers N` and `experiments run --workers N` build the feature rows one season at a time in up to N worker processes.
Each worker opens its own read-only connection to the store file, so no worker writes.
Each worker reads the committed rows of its own snapshot, so no writer may change the store during a backtest.
`daily-prepare.sh` runs the backtest after the refresh, inside the one concurrency group that serializes writers, and `run_backtest` refuses `workers` above 1 for a connection with uncommitted changes.
A failed unit stops the running workers at once, so a bad row in one build does not wait for the other build to finish.
An error of this package keeps its type and gets the unit name in its message.
The merge of the results has one fixed order: season, then game start, then game ID (`merge_scored`), because the block bootstrap depends on the order of its input.

### Measured units

Measured on 2026-11-20 on a copy of the real store (`~/.cache/hockey-model/real.db`, upgraded), one Mac, 4 workers for the parallel run.

| Unit | Seconds |
| --- | --- |
| build actual rows | 1214 |
| build guess rows | 3239 |
| validate elo, elo+xg_form, elo+goalie_gsax, elo+rest (each) | 0.1 |
| walk-forward fit before 2024-25 and before 2025-26 (each) | 0.1 |
| score home_rate, elo, model_actual, model_guess incl. bootstrap (each) | 0.1 |
| final `fit` (builds the actual rows again) | 1213 |
| total, 1 worker | 4454 |
| total, 4 workers | 3252 |

The two feature builds are more than 99.9% of the time of the backtest.
The final `fit` that follows in `daily-prepare.sh` builds the actual rows again, which costs as much as the first build. The season shards below cut that cost too (#289).
Every fit and every bootstrap run takes a tenth of a second, so a process pool for them costs more than it saves.
They stay named units and run in the main process.
The JSON report and the per-game CSV were byte-identical for 1 and for 4 workers on the real store (checked with `cmp`).

### Season shards (#286)

Measured on 2026-11-20 on the same copy of the real store, 4 workers.
Each season is one unit for each goalie mode, so there are 20 units for the 10 seasons that the backtest builds.

| Unit | Seconds |
| --- | --- |
| build actual rows, one season | 84 to 126 |
| build guess rows, one season | 232 to 350 |
| total, 4 workers, season shards | 1262 |

The sharded run is 3.5 times faster than the serial run (4454 s), and 2.6 times faster than the run with one worker for each full build (3252 s).
The report and the per-game CSV were byte-identical to the serial run on the real store (checked with `cmp`).
The golden test runs the sharded build too, and its canonical hashes match the committed ones.

### Final fit (#289)

The final `fit` goes through the newest season that holds a final game, and the backtest stops at the last scored season.
The two row sets differ, so the fit cannot reuse the rows of the backtest.
`fit --workers N` builds its rows with the same season shards (`actual_rows`) instead.

| Step | Seconds |
| --- | --- |
| `fit`, 1 worker (full build) | 1213 |
| `fit`, 4 workers (season shards) | 366 |

The saved weights were byte-identical for the two fits on the real store.
A prepare of a new day takes about 27 minutes with 4 workers: the backtest at 1262 s and the fit at 366 s, down from about 95 minutes.

### Season row cache (#304)

The rows of a finished season do not change when a new day arrives, so a run reads them from a file and builds only the seasons whose key changed.
`row_cache.SeasonFile` holds one file for one goalie mode, one feature list, and one season, in the `season-rows` directory beside the store file.
The file name carries the key of the season (`season_key.season_keys`).
The workflow already saves the directory of the store, so the cache lives as long as the store.
The files are JSON (`row_codec`), so a damaged file fails with a named error and never runs code.
The decode checks the exact type of every field and refuses NaN and infinity.
A file counts as a hit only when its rows are the final games of its season in start order, as the store lists them.
A file that is damaged, empty for a season that has games, a link, or the file of another season is a miss: the season is built again and the file is written again.
A file that cannot be written is a warning, because the run does not need the cache to finish.
The write goes to a temporary file from `mkstemp` and is then renamed, so no link can sit in its place.
A worker writes the file of its own season when it finishes, so the seasons that finished survive a run that fails.
Before it builds, a worker computes the key of its season on its own connection, and it stops when that key differs from the key of the parent, so it never saves rows under a key that the store no longer holds.
The key covers the backtest modes only, and `SeasonFile` refuses the reads mode, which also reads `starter_reads`.

#### The key

The rows of a season read the final games before each of its games, so the rows depend on the games, the MoneyPuck rows, the boxscore goalie rows, and the goalie catalog of the store.
They also start from the Elo ratings before the season, which every earlier game moves.
The key of a season is therefore a running hash over the data of every season from the first game through that season.
A changed old game changes the key of its own season and of every later season.
A new game of the newest season changes the key of that season only.
Four more inputs go into every key: the feature list in its order, the burn-in season, the goalie rows of the catalog, and the hash of the Python and SQL files of the package.
The hash of the package makes every code change a miss, so the first run after a merge builds every season once.

| Input | What the hash covers |
| --- | --- |
| `latest_final_games` | every column except the read time, in season, start, and game order |
| `latest_mp_team_games`, `latest_mp_goalie_games` | the rows of final games, without the read time |
| `goalie_games` | the boxscore goalie rows of final games, with the read time, which is written once |
| `latest_players` | the goalies and every player who started a game in goal, without the read time, for all seasons |
| `source_reads`, `injury_reads` | the injury list reads, by the season whose last game starts at or after the read |

The guess build reads the injury list before each game, so the key covers the injury reads.
`test_a_build_reads_only_tables_that_the_key_covers` records the SQL of both builds, and it fails when a build reads a table that `COVERED_TABLES` does not name.
A table that is added to the key needs a change case in `test_season_key.py`, which fails when a value of that table moves and the key does not.
A read of the injury list after the last game of the newest season reaches no row, so it changes no key.

The goalie catalog is not bound to a season, because a build reads the team of a goalie now and looks a name up among all goalies of the league.
A new goalie, or a goalie who changes team, therefore changes every key, and the next run builds every season.
A skater who never played in goal changes no key.

#### Different feature lists

The backtest builds all candidate features, and the fit builds the features that passed the joint test.
The rows differ in their notes, so each feature list has its own file for a season, and a new key replaces only the file of its own list.

Only the sharded build reads the cache, so `--workers 1` builds every season (#305).

#### Rebuild by hand

`daily-prepare.sh --force` deletes the `season-rows` directory before it builds, so a wrong file cannot survive a forced rebuild.
The directory is the one beside the file that a link to the store names, because SQLite resolves the link.
The golden test also runs the sharded backtest with a warm cache, and its canonical hashes do not move.

#### Measured

Measured on 2026-11-22 on one Mac with 4 workers, on a copy of the real store (`~/.cache/hockey-model/real.db`, upgraded).
The no-cache rows match the season shard table and the final fit table above.
The cache holds 34 files and 21 MB for the backtest and the fit.

| Step | Seconds |
| --- | --- |
| backtest, no cache | 1280 |
| backtest, every season cached | 2 |
| backtest, one game fewer in the newest season (a season that the backtest does not score) | 2 |
| backtest, one game fewer in the last scored season | 332 |
| fit, no cache | 374 |
| fit, every season cached | 2 |
| fit, one game fewer in the newest season | 21 |
| fit, one game fewer in the last scored season | 121 |

A day that adds games to the newest season changes the fit of that season only, because the newest season is after the last scored season of `DEFAULT_PLAN`.
Such a day reads the backtest from the cache and builds one season of the fit, so the two steps take 23 s where they took about 1630 s.
The report, the per-game CSV, and the fit of the incremental runs were byte-identical to a run with no cache on the same store (checked with `cmp`, and with `jq` for the fit without its `fit_id`).
The seasons that the cache does not hold cost the same as before, so a code change, a new goalie, or a changed old game gives one full build.
The CI run time of the `daily-prepare` job is not measured here (#154 holds the baseline).

### Target

The first version of the parallel run used one worker for each full build, and the guess build at about 3251 s bounded it.
The season shards of #286 removed that bound.
One cheap pass over the results records the Elo ratings before the first game of each season (`season_boundaries`).
Each season of each goalie mode is then a unit that starts from its boundary ratings, and joining the units in season order gives the rows of the full build (`build_sharded`).
`PARALLEL_TARGET_S` in `backtest/runner.py` is 1500 s, the sharded run with 4 workers on the real store plus a margin.
The run prints its total against that target.
#224 changes the cost of every fit unit, but the fits are 0.1 s, so it does not change this table.

