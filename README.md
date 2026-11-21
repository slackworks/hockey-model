# hockey-model

Player stat and game prediction interfaces for hockey. The package gives data
and model adapters one typed API for player identity, NHL or fantasy rosters,
stat projections, matchups, and playoff likelihood.

## Python API

Resolve names strictly through a catalog. Full-name matching ignores case and
accents; ambiguous names raise an error that lists canonical IDs.

```python
from hockey_model import Catalog

catalog = Catalog(players_from_your_source)
player = catalog.resolve("Zdeno Chara")
```

Project a player stat with an implementation of `StatModel`:

```python
from hockey_model import project_player

goals = project_player(player, "goals", stat_model)
print(goals.point, goals.low, goals.high, goals.level)
```

Use the same `Roster` type for fantasy and NHL teams. Team uncertainty comes
from a `JointModel`, not from adding player intervals.

```python
from hockey_model import Roster, Slot, project_team

roster = Roster(
    "my roster",
    tuple(players),
    {player.canonical_id: Slot.C for player in players},
)
team_goals = project_team(roster, "goals", stat_model, joint_model)
```

Match any two rosters with a `MatchupModel`:

```python
from hockey_model import matchup

result = matchup(home_roster, away_roster, matchup_model)
print(result.home_win, result.away_win)
```

Every public entry point checks what a model returns before it returns it: a projection must be a `Projection` at the requested level with `low <= point <= high`, a count or goals-against average cannot be negative, and a fractional rate or a probability must be in [0, 1].
A bad result raises `ProjectionError` (or `ProbabilityError` for a probability), and the library never clamps it.
The player, team, and matchup model protocols contain no production model or
data-source choice. An embedding application supplies those adapters.
`FixedStatModel` is available for deterministic examples and tests.
The NHL game win model is the one model that ships (see below).

## NHL game win model

`game_win_probability()` returns the win chance of each side of one NHL game, the expected goals of each side with an interval, and the inputs that were missing.
The model reads only the local store and makes no network request.

```python
from datetime import UTC, datetime

from hockey_model import Game, game_win_probability
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.store.db import connect
from hockey_model.store.fits import FitRef, load_fit

conn = connect(path_to_store)
model = NhlGameModel(conn, load_fit(conn, FitRef(label="live")))
game = Game(2026020236, "TOR", "UTA", datetime(2026, 11, 4, 0, 0, tzinfo=UTC))
result = game_win_probability(game, model)
print(result.home_win, result.away_win, result.made_after_start)
for gap in result.missing:
    print(gap.kind, gap.team, gap.note)
```

The example imports internal modules, so it is for the repository's own tools and tests.
An outside consumer calls `open_store` and `predict` instead, as described in [Consumer API](#consumer-api).
`predict` returns the same `GamePrediction` fields.

The model is the results-only Elo of sportsbettr plus three features: MoneyPuck expected-goals form, the starting goalie's goals saved above expected, and rest days.
A fitted logistic regression sets the weights.
`made_after_start` is true when the prediction time is at or after the puck drop.
A caller must never treat such a result as a pregame read.
The starting goalie comes from the first confirmed ESPN or Daily Faceoff read, then the newest likely read, then the goalie with the most recent starts.
The result lists a goalie that no source confirmed.
`degraded_inputs` lists each form feature of the fit that was set to the neutral value `NEUTRAL_FORM`, because a team had no MoneyPuck games before the game.
A form that was measured, even when it measured zero, is not listed.
The command line prints the same field, and each game in the daily report JSON carries it.

The form reader joins a MoneyPuck row to a final game, so a row whose game is not in the store is dropped.
The model adds a `stale_source` note when a team's newest usable MoneyPuck game is older than its newest final game.
The note names the number of stored games in the gap and the two game ids.
A MoneyPuck file is weekly, so a recent download can still stop several games short.

`training` names the fit and whether the game trained it: `in_sample`, `out_of_sample`, or `unknown`.
A game is `in_sample` when its season trained the fit and it started no later than the newest game that the fit read.
Such a result is not a forecast: the fit has seen the game's outcome.
`unknown` means the saved fit does not record the season it trained through, so a reader must not read it as out of sample.
`game`, `daily`, and the consumer API carry the same field. The daily report Markdown puts an `in_sample` note on the game row.

The model uses no price data.
It does not say whether the model beats a betting market.
The backtest (below) scores it against game results and the results-only Elo only.

### The store

All data sits in one SQLite file.
Set `HOCKEY_MODEL_DB` to its path.
An unset or empty variable is an error that names it.

```text
HOCKEY_MODEL_DB=~/.cache/hockey-model/store.db hockey-model refresh --backfill-from 2014-10-01
HOCKEY_MODEL_DB=~/.cache/hockey-model/store.db hockey-model fit --through-season 20252026 --features xg_form,rest --label live
HOCKEY_MODEL_DB=~/.cache/hockey-model/store.db hockey-model game 2026020236
HOCKEY_MODEL_DB=~/.cache/hockey-model/store.db hockey-model backtest --games-out games.csv
```

- `refresh` reads every source and appends each read with its fetch time. It exits 1 when a source failed, and it prints what each source wrote, the errors, the credits, and `backfill_complete`. The first backfill makes about 16,000 NHL requests at two a second, so it takes hours. A backfill reads one year at a time, newest year first. `--budget-seconds N` stops it after N seconds, and `backfill_complete` is then false. The budget also stops the MoneyPuck files: a run reads one stale MoneyPuck file at least, and it stops before the next one when the time is spent. A refresh skips a score day, boxscore, or MoneyPuck file whose reads failed in a row, and the notes name it. A URL becomes a skipped gap after `ABANDON_AFTER_FAILED_READS` failed reads in a row that span `ABANDON_MIN_SPAN_DAYS`, or after that many in a row when each failed with a permanent HTTP status such as 403. A live feed is read on every run instead: a failed read of a feed that is already a known gap records no error, so it does not set the exit code, and each prediction names it as a stale source. One good read clears that gap with no flag needed. `--retry-skipped` reads every skipped unit again once, and one good read clears a skip. Run it again after a stop or an error: it skips what it already has. A game that started long ago and is still not final stays in the worklist: its score day is read again after a wait, at most `MAX_UNRESOLVED_DAYS` days in one run, and the notes name the game. After the MoneyPuck files, the notes count the rows that match no stored game and the rows whose game is not final yet. `refresh --starters-only` reads no MoneyPuck file, so it writes neither note.
- `refresh --starters-only` reads only the schedule, the goalie sources, and the injuries. The report run uses it after the prepare run's full refresh.
- `fit --through-season YYYYYYYY --features a,b [--workers N]` fits the weights on every game from the 2015-16 season through that season and saves the fit. `--features` is required, because no default feature list is approved. Pass the `live_features` of the latest backtest report, such as `xg_form,rest` for the November 2026 report below. `--features ""` saves the Elo alone, which is the right fit when the joint test fails. `--label live` names the production fit: `game`, `daily`, and the consumer serve the fit with that label, and a fit with no label cannot replace it. A fit refuses, with exit 2 and no saved fit, when a selected feature is imputed in more than 10% of its training rows. The `imputation` list in the output and in the backtest report gives the imputed count and share of each selected feature. The candidates are `xg_form`, `goalie_gsax`, and `rest`.
- `game GAME_ID [--level L] [--fit-id N]` prints the prediction of one stored game with the fit labelled `live`, or with fit `N` when `--fit-id` is given.
- `backtest [--games-out PATH] [--workers N]` runs the walk-forward backtest and prints a JSON report. `--workers N` builds the feature rows one season at a time in up to N processes, each with its own read-only store connection, and logs the wall time of each unit on stderr. `experiments run` and `fit` take the same option. The report and the CSV are the same for every N. `--games-out` writes one CSV row for each scored game and run.
- `store upgrade [--db PATH] [--if-missing {fail,skip}]` moves an older store to the current schema, one version at a time.
It keeps every row and every fit ID.
The default `--if-missing fail` names a store file that does not exist.
`--if-missing skip` exits 0 and reports that there is no store yet, for a first run that writes one later.
It first writes a backup beside the store, named `<store>.v<version>.bak`, and it never overwrites an earlier backup.
The daily workflows delete that backup before they save the store to the cache, so a later no-op report can name a backup that is no longer there.
`--if-missing skip` reports `from_version: null` and a `note`.
The change runs in one transaction, so a failure leaves the store as it was.
The two MoneyPuck shot-attempt columns are NULL for the rows that version 1 wrote. No feature reads them.
To restore, stop every writer, copy the backup over the store file, and delete any `-wal` and `-shm` files beside it.
Opening a store never migrates it, so a consumer on an old store fails and names this command.
Opening a store also checks the shape of its tables against the stamp.
A stamp that names a version the tables do not hold fails at open and names the missing column, instead of a later `no such column`.
The daily prepare and report scripts run this command on the store that the workflow cache restores.
Upgrade the producer first, then the consumers.
The report lists `coverage`: the scored games of each season and game type, with the expected count of a regular season (`EXPECTED_REGULAR_GAMES` in `game_model/seasons.py`) and the stored games of that season that never finished.
`eligible` is false when a listed regular season is under `MIN_SEASON_COVERAGE` of its expected count, or when the scored games hold fewer than `MIN_INDEPENDENT_BLOCKS` independent blocks for the bootstrap.
Then `success` is false, `live_features` is empty, the interval is `null`, and `ineligible_reasons` says why. The scores stay in the report as descriptions.
- `store gaps --live [--db PATH]` prints, as JSON, each live feed (the ESPN goalie and injury feeds and Daily Faceoff) that is a known gap. Each item has `source`, `url`, `gap_since`, `age_hours`, `last_ok`, and `over_limit`, which is true when the gap is older than `LIVE_FEED_MAX_AGE`. A `feeds` list names the newest read of each feed (`newest_read`, `age_hours`, `over_limit`), and a feed with no read, or an old newest read, is over the limit. The schema is `src/hockey_model/live_gaps.schema.json`. The `daily-report-check` workflow runs `scripts/check-live-feeds.sh`, which fails for each gap that is over the limit.
- `daily [--date YYYY-MM-DD] --out-dir DIR [--level L]` predicts every game of one game day, and writes `report-DATE.md` and `report-DATE.json`. A game day is the Eastern date that the NHL schedule uses, so a 7 pm ET slate that starts after midnight UTC stays in one report. Game starts stay UTC instants. The day defaults to the game day that holds now. A game that already started is still predicted, with its inputs taken as of the puck drop and a `late_run` note. A game that is over is predicted the same way, so a late first report is full. A postponed game gets no prediction. The JSON copy names the model in `model`: the fit features, a SHA-256 of the fit weights (`fit_sha256`), and the code commit from `GITHUB_SHA` (`code_sha`, null outside Actions).
- `report-due [--date YYYY-MM-DD] [--force] [--prep-started ISO]` prints whether the first puck drop of the game day is still ahead of the run, plus `--margin-seconds`, so the current run may publish the day's report. Every trigger that starts before the first puck drop is due. The first run that publishes creates the release. Later runs before the first puck drop add a new build, and only when the inputs differ from the newest published report (`inputs_sha256` in the report JSON), so a reader can see the release change until then. After the puck drop, runs skip the day. A run that starts after the puck drop publishes the first report of the day late, with a warning. A game day with no games publishes nothing. The output also says `puck_drop_passed`, so a run that finds no prep marker skips before the first puck drop and fails after it. `--prep-started` names the prepare run that holds the store group, and `prep_blocks_report` says that its timeout plus the report budget reaches past the puck drop, so the gate skips the run with reason `prepare-wait`. Each build keeps its own `report-DATE-HHMMSSZ` files, so no published version is replaced. The report runs at minute `TRIGGER_MINUTE_UTC` of every hour, and the cron in `.github/workflows/daily-report.yml` MUST list the same minute. The prepare runs at minute 14 of every hour. The design is in [docs/design/daily-report-triggers.md](docs/design/daily-report-triggers.md).
- `report-compare --built PATH --published PATH` prints whether a built report JSON has the `inputs_sha256` of the newest published report. The answer has `unchanged`, `baseline`, and `reason` (`unchanged` or `replace`). `scripts/daily-report.sh --skip-unchanged` runs it before a publish.
- `report-decide` prints the publish step of one trigger from the facts that `scripts/report-gate.sh` read: `--due`, `--games`, `--date`, `--force`, `--dry-run`, and `--published`. With `--published unknown` it prints `lookup_needed`. When that is true, read the release and ask again with `--published true` or `false`. It prints `due`, `publish`, `reason`, `lookup_needed`, and `warning`.
- To test the report by hand, run `gh workflow run daily-report -f force=true -f dry_run=true`. The run builds the report for the current game day, skips the trigger slot and the published-release check, and uploads the report as an artifact. It publishes no release.
- `fit` without `--through-season` fits through the newest season that holds a final game in the store, and records that season.
An explicit `--through-season` newer than the newest stored final game is refused, so the saved cutoff never names a season that trained nothing.

Two workflows publish the report. `daily-prepare` runs `scripts/daily-prepare.sh` every hour: it refreshes every source, runs the backtest, and fits the model on the `live_features` of that backtest. A backtest report without `live_features` stops the run. `daily-report` runs `scripts/report-gate.sh` at each trigger. When the gate is due, it runs `scripts/daily-report.sh`, which refreshes only the starters and predicts, and then `scripts/publish-daily-report.sh`. The release is tagged `reports-DATE` and holds the daily report, the index of its builds (`index-DATE.json`), the backtest JSON (`backtest-DATE.json`), and the backtest per-game CSV (`backtest-games-DATE.csv`). When the prepare has not finished, the report run skips before the first puck drop and retries at the next hour. After the first puck drop, it stops with exit code 1 and names the fix: run the `daily-prepare` workflow with that date, then run the report again.

The store lives in the Actions cache. A cache miss, such as after eviction, starts a full backfill. Each `daily-prepare` run reads for `BACKFILL_BUDGET_S` seconds, one year at a time and newest year first, so a full backfill takes several runs. A run that stops early exits with code 75 and saves the store. The backfill writes a marker file next to the store when it finishes. A store without the marker is partial, so the next run resumes the backfill. `daily-prepare` runs the backtest and the fit only after the marker exists. A day that has its `prepared-DATE` marker and a `backtest-DATE.json` with a `live_features` list of names is already prepared, so a second `daily-prepare` for it refreshes the store, says that it skipped, and exits 0. `daily-prepare.sh --force`, or the `force` input of a manual `daily-prepare` run, rebuilds the day on purpose. A scheduled run never forces. A day that already has its release gets a new build only when `report-compare` finds new inputs, and only before the first puck drop. `daily-report` does not run `daily-prepare`. A day with no `prepared-DATE` marker stops the report with exit code 1, and the message names the `daily-prepare` workflow to run.

### The published report

A consumer reads the release assets of `reports-DATE`, and not the store.
The files are a contract.
`src/hockey_model/daily_report.schema.json` is the JSON Schema of the report, and `src/hockey_model/report_index.schema.json` is the JSON Schema of the index.
Both ship in the wheel.
`daily` checks the report against its schema before it writes a file, and `scripts/publish-daily-report.sh` checks it again before it uploads.
An invalid report stops the run and names the JSON path and the schema.
[docs/design/report-contract.md](docs/design/report-contract.md) says when `schema_version` changes.

| Asset | What it holds |
| --- | --- |
| `report-DATE.json` | The newest build of the day. |
| `report-DATE-HHMMSSZ.json` | One build, named by its `generated_at` time (UTC). A published version is never replaced. |
| `index-DATE.json` | One entry for each build, oldest first. A rebuild adds its entry and drops none. |

A report has these fields.
Every field is always present.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | The version of this shape. It is 1. |
| `date` | text | The Eastern game day, `YYYY-MM-DD`. |
| `generated_at` | text | The time of the run, ISO 8601 UTC with microseconds. |
| `inputs_sha256` | text | A SHA-256 of what a reader sees, without the time of the run. Equal values mean equal numbers. |
| `level` | number | The interval level of the goal ranges, between 0 and 1. |
| `credits` | list of text | The data sources that the predictions used. |
| `model` | object or null | `features` (fit features), `fit_sha256` (SHA-256 of the fit weights), and `code_sha` (the commit, null outside Actions). Null when the run did not name a fit. |
| `training_gaps` | list of text | Source gaps in days older than the previous season. The measured case, a gap of up to a week two seasons back, moved a win chance by at most `OLD_GAP_MEASURED_SHIFT_PP` percentage points. |
| `games` | list | One object for each game that starts on the day. |

A game has these fields.

| Field | Type | Meaning |
| --- | --- | --- |
| `game_id` | integer | The NHL game ID. |
| `home`, `away` | text | The team codes. |
| `start_utc` | text | The puck drop, ISO 8601 UTC with microseconds. |
| `state` | text | The NHL game state, such as `FUT`, `LIVE`, `OFF`, or `PPD`. |
| `skipped` | text or null | The reason the game has no prediction. A postponed game holds `game is postponed (state PPD); no prediction`. Null when the game has a prediction. |
| `home_win`, `away_win` | number or null | The win chances. Null when `skipped` holds a reason. |
| `made_after_start` | boolean | True when the run made the prediction after the puck drop. |
| `missing` | list | Inputs that were absent or weak: `kind`, `team` (text or null), and `note`. |
| `degraded_inputs` | list | Features set to a neutral value for a team with no data: `feature`, `kind`, and `teams`. |
| `training` | object or null | `fit_id`, `fit_through_season`, and `membership` (`in_sample`, `out_of_sample`, or `unknown`). Null when `skipped` holds a reason. |

`missing.kind` and `degraded_inputs[].kind` hold one of `goalie_unconfirmed`, `goalie_unknown`, `goalie_conflict`, `xg_form`, `form_games_left_out`, `goalie_form`, `stale_source`, `late_run`, or `rest_unknown`.

One report, with one game:

```json
{
  "schema_version": 1,
  "date": "2026-11-04",
  "generated_at": "2026-11-04T20:20:13.000000+00:00",
  "inputs_sha256": "9f2c4b6e0a1d3f58c7e2a4b6d8f0123456789abcdef0123456789abcdef01234",
  "level": 0.8,
  "credits": ["Data from MoneyPuck.com."],
  "model": {
    "features": ["xg_form", "rest"],
    "fit_sha256": "b1d5c3e7f9a2b4d6e8f0a1c3e5d7f9b2a4c6e8d0f1b3d5e7a9c1b3d5f7e9a0c2",
    "code_sha": "0123456789abcdef0123456789abcdef01234567"
  },
  "training_gaps": [],
  "games": [
    {
      "game_id": 2026020201,
      "home": "TOR",
      "away": "BOS",
      "start_utc": "2026-11-05T00:00:00.000000+00:00",
      "state": "FUT",
      "skipped": null,
      "home_win": 0.55,
      "away_win": 0.45,
      "made_after_start": false,
      "missing": [
        {"kind": "goalie_unconfirmed", "team": "TOR", "note": "starter is expected, not confirmed"}
      ],
      "degraded_inputs": [],
      "training": {"fit_id": 3, "fit_through_season": 20252026, "membership": "out_of_sample"}
    }
  ]
}
```

The index of one day:

```json
{
  "schema_version": 1,
  "date": "2026-11-04",
  "builds": [
    {
      "file": "report-2026-11-04-202013Z.json",
      "generated_at": "2026-11-04T20:20:13.000000+00:00",
      "code_sha": "0123456789abcdef0123456789abcdef01234567",
      "fit_sha256": "b1d5c3e7f9a2b4d6e8f0a1c3e5d7f9b2a4c6e8d0f1b3d5e7a9c1b3d5f7e9a0c2",
      "inputs_sha256": "9f2c4b6e0a1d3f58c7e2a4b6d8f0123456789abcdef0123456789abcdef01234",
      "schema_version": 1
    }
  ]
}
```

`report-index --report PATH --date YYYY-MM-DD [--existing PATH] --out PATH` checks a report against its schema and writes the index with that build added. It refuses a report of another day. It prints `out`, `builds` (the count), and `file` (the name of the added build, `report-DATE-HHMMSSZ.json`, from `generated_at`).
A day published before the index existed has no entry for its older builds.

### Data sources

| Source | Data |
| --- | --- |
| NHL API (`api-web.nhle.com`) | Schedule, results, boxscores, and the actual starting goalie |
| MoneyPuck CSV files | Expected goals for each team game and each goalie game |
| ESPN (`site.api.espn.com`, unofficial) | Probable starting goalies and injuries |
| Daily Faceoff (`dailyfaceoff.com/starting-goalies`) | Starting goalies |

Data from MoneyPuck.com.
MoneyPuck allows its data for non-commercial use only, and asks for credit wherever the data is shown.
hockey-model downloads only the CSV and ZIP files that the MoneyPuck data page lists, and every output that uses them carries the credit.
Read the MoneyPuck terms before any commercial use.
ESPN does not support its endpoints, so hockey-model fetches them sparingly.
The HTTP client waits between requests to one host, retries rate limits, and sends the user agent `hockey-model/<version>`.

### Backtest result

The November 2026 backtest scored 2,792 games from the 2024-25 and 2025-26 seasons.
The joint test passed: the live model beats the results-only Elo on log loss, 0.6763 against 0.6876.
The 90% interval (5th to 95th percentile) of the difference is -0.0144 to -0.0083. These figures come from the first backtest, before the bootstrap ends were made equal (see CHANGELOG.md).
The live features are expected-goals form and rest.
The guess run vetoed goalie goals saved above expected.
MoneyPuck's expected-goals model may have trained on the scored games, so the gain may look larger than it was.
Issue #77 tests this with a form built from shot-attempt counts.
A reviewer then scored the model against the last pre-game DraftKings line on matched regular season games.
The model is level with the line in 2024-25 (0.6633 against 0.6604) and worse in 2025-26 (0.6911 against 0.6826, interval +0.0035 to +0.0140).
The model does not pass the blend gate for betting.
The full report is in [docs/backtest/2026-11-nhl-game-model.md](docs/backtest/2026-11-nhl-game-model.md).

### Experiment registry

Each experiment is one JSON record in `experiments/`.
A record states the hypothesis, the data cutoff, the baseline, the cohort, the
minimum games, the promotion criterion, and the failure outcome.
A record is frozen once registered: only `status` and `result` may change, and
`hockey-model experiments check` compares the record with the commit that
registered it.

An exploration record scores the seasons through 2025-26 and can run as many
times as you like.
It scores every game type the store holds, playoff games included, and it includes the two seasons that the backtest report already scored.
A confirmation record scores only games after its cutoff in the 2026-27 regular
season and later, and it runs once.
The run refuses when the cohort holds fewer games than the record requires.
A run whose interval is wider than the effect the record expected is
inconclusive, not rejected.
The promotion bar of a confirmation record follows its look number `k`: the interval cuts the tail `BASE_TAIL / (k * (k + 1))`, so the tails of all looks sum to less than `BASE_TAIL`.
A record that will not run is set to the status `withdrawn`. It spends no look, holds no result, and cannot return to another status.
See [docs/design/promotion-bar.md](docs/design/promotion-bar.md).

Run the registry commands from the repository root.
The records live in `experiments/`, relative to the working directory. The schema
that validates them is `src/hockey_model/experiments/schemas.py`.

```text
hockey-model experiments check
hockey-model experiments run 0002-v1-forward-test --games-out games.csv
```

The per-game CSV of a confirmation run is committed under
`experiments/results/`, and the record stores its path relative to the repository root and its SHA-256 hash.
`experiments check` also fails when a record that holds a result is deleted or renamed, when a record that never ran is deleted while a higher confirmation record exists, when its status disagrees with its result, when a committed result was removed or replaced, and when two records share a number.
It reads the git history, so a shallow clone stops with a message that asks for the full history.
Outside a git work tree, and before the first commit, it cannot read the history. The output then has a `notes` entry, `deletion history not checked: ...`, so the missing check is not read as a pass.

## Consumer API

`sportsbettr` and `leaguecheatr` read the store through one supported module.
The consumer never runs SQL, never imports an internal module, and never writes, migrates, fits, or refreshes the store.
`open_store` is the only entry point, and it is exported from the package root.

```python
from datetime import UTC, date, datetime

from hockey_model import FitRef, open_store

with open_store(path_to_store, fit=FitRef(label="live")) as store:
    print(store.store.path, store.store.schema_version)
    for game in store.games_on_day(date(2026, 11, 4)):
        print(game.game_id, game.away, "at", game.home, game.start, game.state)
    result = store.predict(game_id, as_of=datetime(2026, 11, 4, 0, 0, tzinfo=UTC))
    print(result.prediction.home_win, result.fit.features, result.fit.fit_sha256)
```

The consumer imports the supported names from the package root only.
`hockey_model.store.fits` and the other internal modules are not part of the surface, even when a name appears in both places.
`LIVE_LABEL` is `"live"`, and it names the fit that `hockey-model game` and `hockey-model daily` serve.

The store opens read-only, over one read transaction, so every call of one consumer sees one state of the store.
A producer may write at the same time.
Call `refresh_snapshot()` to end the transaction and start a new one.
The fit is pinned when the store opens, and it never changes for the life of the object.

A missing file raises `StoreMissingError`, and the consumer never creates one.
A schema version this package cannot read, a missing view, and an unknown fit or game raise `StoreVersionError`, `UnknownFitError`, or `UnknownGameError`.
Every error names the fix, which is `hockey-model store upgrade` or `hockey-model refresh`, run by the producer.

`result.outcome` is `"full_game_winner"`: one winner after overtime and the shootout.
It is not a regulation, period, prop, futures, or in-play result.
`result.prediction.home_goals` and `result.prediction.away_goals` are marginal intervals for one team's goals.
They are not a joint score distribution: two marginals give no chance of a total, and no chance of a margin.
`result.reconstruction` is `"restatement"`: the inputs are today's rows, not the rows that existed at `as_of`.
A call with an `as_of` therefore does not prove an as-issued historical prediction.
`result.made_after_start` is true when `as_of` is at or after the puck drop.

A game that trained the fit is an in-sample result, and the fit has seen its outcome.
`result.training` names the fit and the membership: `in_sample`, `out_of_sample`, or `unknown`.
`result.training.fit_through_season` is the last season that trained the fit.
A `fit_through_season` of `None` gives `unknown`, not `out_of_sample`.
A fit also records a game cutoff, which is the start of the newest final game that it read.
A game is `in_sample` when its season is the cutoff season or an earlier one, and its start is no later than that game cutoff.
The game cutoff therefore marks the first game of a live fit's own season that the fit did not read.
A game of the season before the first fit season is `out_of_sample`, even when its start is inside the cutoff, because no fit trains on it. The Elo burn-in season is the case that matters.
The season comes from the stored season of the game when the store holds it, and from the Eastern game day of the start otherwise.
An `as_of` earlier than the game does not clear the marker, because the fit still trained on the game.

### Install for a consumer

The repository is public, so the install needs no token.
Pin a tag or a commit, so a consumer never builds from a moving branch.

```text
uv add "hockey-model @ git+https://github.com/slackworks/hockey-model@<tag-or-commit>"
```

A consumer must not use a sibling checkout at a developer-specific absolute path.

## CLI

The player, team, matchup, and playoff commands wrap the same calls and print
JSON. The installed command reports that models are unconfigured until an
embedding application provides `CliRuntime`; it does not hide a second model
stack inside the CLI. The store commands above need no `CliRuntime`.

```text
hockey-model player goals PLAYER_ID
hockey-model team goals roster.json
hockey-model matchup home.json away.json
hockey-model playoffs TEAM_CODE
```

Roster files are JSON objects with `name`, `players`, and optional `slots`.
Player objects use the public `Player` field names, and slot values use the
public `Slot` values.

## Develop

```text
uv sync --locked
uv run pytest
prek install
```

The CI test job and the pre-push hook run `uv run pytest -n auto`, which uses every core.
See `AGENTS.md` for the commands and rules.

## Release

A tag that starts with `v` runs the release workflow (`.github/workflows/release.yml`).
The workflow publishes only after the gate passes.
Release in this order:

1. Rename the `## [Unreleased]` section of `CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD`, and add a new `## [Unreleased]` section above it.
2. Set `project.version` in `pyproject.toml` to `X.Y.Z`.
3. Merge both changes to `main`.
4. Run `uv run python3 scripts/check_release.py --tag vX.Y.Z` on `main`. It prints the version and says if it is a pre-release.
5. Tag the merge commit `vX.Y.Z`, and push the tag.

The jobs and the checks of each job are in `.github/workflows/release.yml`. It refuses a tag on a commit that is not on `main`.
Re-run only the failed jobs after a failure, because a run of all jobs builds the files again, and a file with other bytes stops the publish.
A pre-release version, such as `0.2.0rc1`, makes a pre-release.
The workflow publishes to no package index.
