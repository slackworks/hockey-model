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

- `refresh` reads every source and appends each read with its fetch time. It exits 1 when a source failed, and it prints what each source wrote, the errors, the credits, and `backfill_complete`. The first backfill makes about 16,000 NHL requests at two a second, so it takes hours. A backfill reads one year at a time, newest year first. `--budget-seconds N` stops it after N seconds, and `backfill_complete` is then false. The budget also stops the MoneyPuck files: a run reads one stale MoneyPuck file at least, and it stops before the next one when the time is spent. A refresh skips a score day, boxscore, or MoneyPuck file whose reads failed in a row, and the notes name it. `--retry-skipped` reads those units again once, and one good read clears the skip. Run it again after a stop or an error: it skips what it already has. A game that started long ago and is still not final stays in the worklist: its score day is read again after a wait, at most `MAX_UNRESOLVED_DAYS` days in one run, and the notes name the game. After the MoneyPuck files, the notes count the rows that match no stored game and the rows whose game is not final yet. `refresh --starters-only` reads no MoneyPuck file, so it writes neither note.
- `refresh --starters-only` reads only the schedule, the goalie sources, and the injuries. The report run uses it after the morning refresh.
- `fit --through-season YYYYYYYY --features a,b` fits the weights on every game from the 2015-16 season through that season and saves the fit. `--features` is required, because no default feature list is approved. Pass the `live_features` of the latest backtest report, such as `xg_form,rest` for the November 2026 report below. `--features ""` saves the Elo alone, which is the right fit when the joint test fails. `--label live` names the production fit: `game`, `daily`, and the consumer serve the fit with that label, and a fit with no label cannot replace it. A fit refuses, with exit 2 and no saved fit, when a selected feature is imputed in more than 10% of its training rows. The `imputation` list in the output and in the backtest report gives the imputed count and share of each selected feature. The candidates are `xg_form`, `goalie_gsax`, `rest`, and `attempt_form` (the leak test of #77).
- `game GAME_ID [--level L] [--fit-id N]` prints the prediction of one stored game with the fit labelled `live`, or with fit `N` when `--fit-id` is given.
- `backtest [--games-out PATH] [--candidates {xg,attempt}]` runs the walk-forward backtest and prints a JSON report. `--games-out` writes one CSV row for each scored game and run. `--candidates attempt` replaces `xg_form` with the shot-attempt form, the leak test in #77.
- `store upgrade [--db PATH]` moves a version 1 store to the current schema.
It keeps every row and every fit ID.
It first writes a backup beside the store, named `<store>.v1.bak`, and it never overwrites an earlier backup.
The change runs in one transaction, so a failure leaves the store as it was.
The four MoneyPuck attempt columns are NULL for the rows that version 1 wrote, so the attempt-form features skip those rows.
To restore, stop every writer, copy the backup over the store file, and delete any `-wal` and `-shm` files beside it.
Opening a store never migrates it, so a consumer on a version 1 store fails and names this command.
Upgrade the producer first, then the consumers.
After the upgrade, run `hockey-model refresh --reread-moneypuck` before any attempt-form backtest.
Until that refresh runs, the attempt-form features are empty.
The report lists `coverage`: the scored games of each season and game type, with the expected count of a regular season (`EXPECTED_REGULAR_GAMES` in `game_model/seasons.py`) and the stored games of that season that never finished.
`eligible` is false when a listed regular season is under `MIN_SEASON_COVERAGE` of its expected count, or when the scored games hold fewer than `MIN_INDEPENDENT_BLOCKS` independent blocks for the bootstrap.
Then `success` is false, `live_features` is empty, the interval is `null`, and `ineligible_reasons` says why. The scores stay in the report as descriptions.
- `daily [--date YYYY-MM-DD] --out-dir DIR [--level L]` predicts every game of one game day, and writes `report-DATE.md` and `report-DATE.json`. A game day is the Eastern date that the NHL schedule uses, so a 7 pm ET slate that starts after midnight UTC stays in one report. Game starts stay UTC instants. The day defaults to the game day that holds now. A game that already started is still predicted, with its inputs taken as of the puck drop and a `late_run` note. A game that is over is predicted the same way, so a late first report is full. A postponed game gets no prediction. The JSON copy names the model in `model`: the fit features, a SHA-256 of the fit weights (`fit_sha256`), and the code commit from `GITHUB_SHA` (`code_sha`, null outside Actions).
- `report-due [--date YYYY-MM-DD] [--force]` prints whether the first puck drop of the game day is still ahead of the run, plus `--margin-seconds`, so the current run may publish the day's report. Every trigger that starts before the first puck drop is due. The first run that publishes creates the release. Later runs before the first puck drop replace its files and notes, so a reader can see the release change until then. After the puck drop, runs skip the day. A run that starts after the puck drop publishes the first report of the day late, with a warning. A game day with no games publishes nothing. The output also says `prep_too_early` when the first puck drop is more than the prep window away, so a run that finds no prep marker skips instead of failing. Each build keeps its own `report-DATE-HHMMSSZ` files, so no published version is replaced. The triggers run at minute `TRIGGER_MINUTE_UTC` of each hour in `TRIGGER_HOURS_UTC` in `report_trigger.py`, and the cron in `.github/workflows/daily-report.yml` MUST list the same minute and hours. `scripts/trigger_hours.py` derives them from the season schedule. The design is in [docs/design/daily-report-triggers.md](docs/design/daily-report-triggers.md).
- `report-decide` prints the publish step of one trigger from the facts that `scripts/report-gate.sh` read: `--due`, `--games`, `--date`, `--force`, `--dry-run`, and `--published`. With `--published unknown` it prints `lookup_needed`. When that is true, read the release and ask again with `--published true` or `false`. It prints `due`, `publish`, `reason`, `lookup_needed`, and `warning`.
- To test the report by hand, run `gh workflow run daily-report -f force=true -f dry_run=true`. The run builds the report for the current game day, skips the trigger slot and the published-release check, and uploads the report as an artifact. It publishes no release.
- `fit` without `--through-season` fits through the newest season that holds a final game in the store, and records that season.
An explicit `--through-season` newer than the newest stored final game is refused, so the saved cutoff never names a season that trained nothing.

Two workflows publish the report. `daily-prepare` runs `scripts/daily-prepare.sh` each morning before the first trigger: it refreshes every source, runs the backtest, and fits the model on the `live_features` of that backtest. A backtest report without `live_features` stops the run. `daily-report` runs `scripts/report-gate.sh` at each trigger. When the gate is due, it runs `scripts/daily-report.sh`, which refreshes only the starters and predicts, and then `scripts/publish-daily-report.sh`. The release is tagged `reports-DATE` and holds the daily report, the backtest JSON (`backtest-DATE.json`), and the backtest per-game CSV (`backtest-games-DATE.csv`). When the morning run did not finish, the report run stops with exit code 1 and names the fix: run the `daily-prepare` workflow with that date, then run the report again.

The store lives in the Actions cache. A cache miss, such as after eviction, starts a full backfill. Each `daily-prepare` run reads for `BACKFILL_BUDGET_S` seconds, one year at a time and newest year first, so a full backfill takes several runs. A run that stops early exits with code 75 and saves the store. The backfill writes a marker file next to the store when it finishes. A store without the marker is partial, so the next run resumes the backfill. `daily-prepare` runs the backtest and the fit only after the marker exists. A day that already has its release is skipped. A second publish for the same day fails. Delete the release first to publish the day again. `daily-report` does not run `daily-prepare`. A day with no `prepared-DATE` marker stops the report with exit code 1, and the message names the `daily-prepare` workflow to run.

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

Run the registry commands from the repository root.
The records live in `experiments/`, relative to the working directory. The schema
that validates them is `src/hockey_model/experiments/schemas.py`.

```text
hockey-model experiments check
hockey-model experiments run 0001-v1-forward-test --games-out games.csv
```

The per-game CSV of a confirmation run is committed under
`experiments/results/`, and the record stores its path relative to the repository root and its SHA-256 hash.
`experiments check` also fails when a record that holds a result is deleted or renamed, when its status disagrees with its result, when a committed result was removed or replaced, and when two records share a number.
It reads the git history, so a shallow clone stops with a message that asks for the full history.

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

See `AGENTS.md` for the commands and rules.
