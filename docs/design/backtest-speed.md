# Backtest speed (#138)

## Goal

Make `hockey-model backtest` and `hockey-model experiments run` faster with no change in any output.
The JSON report and the per-game CSV MUST stay byte-identical for the same store.

## Step 1: the golden check

Build this before any speed change, in its own pull request.

1. `scripts/backtest_golden.py --db PATH --out DIR` runs `hockey-model backtest --games-out` and writes `report.json`, `games.csv`, and their SHA-256 hashes.
2. A test runs the same backtest on the seeded store (`tests/seeding.py`) and compares the hashes with the values committed in `tests/fixtures/backtest_golden.json`. A change that moves a hash fails the test.
3. A change that is meant to move a number is a model change. It updates the golden file in a separate commit, and the pull request says why.

Run the script on the real store (`~/.cache/hockey-model/real.db`) before and after each speed change too, and paste the two hashes in the pull request.
The seeded store is small, so a real-store check catches an order or rounding change that the seed does not reach.

## Step 2: the profile

Run `uv run python3 -m cProfile -o .tmp/backtest.prof -m hockey_model.cli backtest` on the real store.
Print the 30 most expensive functions by cumulative time with `pstats`.
Write the table in the issue, in these groups: feature rows (SQL and Python), Elo replay, the logistic fits, the validation fits, the bootstrap, and the CSV write.
Also time one `experiments run 0001-v1-forward-test` on the same store.

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
