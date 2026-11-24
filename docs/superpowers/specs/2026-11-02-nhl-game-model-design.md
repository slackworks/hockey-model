# NHL game win model design

## Intent

sportsbettr bets on NHL games on Kalshi.
It needs the chance that each team wins a game, from a model that does not copy the market.
sportsbettr then compares that chance with the price and decides the bet.

hockey-model has the public API for this (see `2026-11-02-api-ux-design.md`), but no data and no model.
leaguecheatr and sportsbettr each hold NHL data code, and parts of it overlap.
This project moves the useful NHL data code into hockey-model, builds a game win model on it, and adds a backtest that proves the model is better than a simple Elo rating.

## Goals

- A caller asks for one NHL game and gets the win chance for each side, the expected goals for each side, and a list of the inputs that were missing.
- hockey-model fetches and stores its own NHL data.
- A backtest scores the model against real game results and against simple baselines.
- The backtest shows the value of each model feature on its own, and tests the full model once against the Elo.

## Non-goals

- Market prices of any kind. hockey-model does not read Kalshi, The Odds API, or sportsbook lines, and no model input comes from a price. The comparison with the market stays in sportsbettr's backtest.
- Bet sizing, fees, profit, and orders. These stay in sportsbettr.
- Changes to sportsbettr or leaguecheatr. Each consumer moves to hockey-model in its own later project (see "Follow-up work").
- Changes to the fantasy roster `matchup()` model.
- Daily Faceoff line combinations and power-play units. The v1 model has no input that uses them.

## Anti-goals

- A model input must not use data from after the game start. One leak makes every backtest number false. The one accepted exception is the MoneyPuck expected-goals model (see "The backtest").
- A missing input must not look like a complete prediction. The result always names what was missing.
- A source that is down or changes shape must not stop the prediction, and must not fail silently. The fetch fails loudly, and the model uses the data stored before.
- No fetch pattern that a source operator would call abuse.

## The API addition

The existing `matchup(home, away, model)` call stays as it is, for two fantasy or NHL rosters.
An NHL game adds a start time and starting goalies, so it gets its own input.

### `Game`

- The NHL game ID.
- The home and away team codes.
- The scheduled start time, as a timezone-aware UTC time.
- The starting goalie for each side, as a `GoalieStart`, or none when no source names one.

### `GoalieStart`

- The goalie, as a `Player`.
- A status: confirmed, likely, or unknown.
- The source that gave the status: ESPN, Daily Faceoff, or the NHL boxscore.
- The time of the read.

A goalie supplied on a `Game` must have a read time before the earlier of `as_of` and the game start.
Otherwise the model raises `HockeyModelError`, because the read was not known at that time.
A supplied likely or unknown start gives the same missing-input note as a stored one.

The goalie status type and the starting-goalie query are public.
Fantasy rosters also have goalies and likely starters, so a fantasy caller uses the same query and the same status values.

### `game_win_probability(game, model)`

It returns a result with these fields:

- The home and away win chances. They sum to one. Overtime and shootout wins count for the side that takes them, the same rule as `matchup()`.
- The expected goals for each side, as a `Projection` with an interval.
- The missing inputs: a tuple of named values, each with a short note. An empty tuple means every input was present.
- `made_after_start`: true when the prediction time is at or after the game start. A caller must never treat such a result as a pregame read.

The model behind the call is a protocol, the same as the other model boundaries in `models.py`.
The protocol takes the time of the prediction, so a backtest can ask for a prediction "as of" a past time.

**My call:** the call raises an error when the model returns chances that are not finite, are outside [0, 1], or do not sum to one. This repeats the `matchup()` check.

## Data sources

| Source | Data | Use | Terms |
| --- | --- | --- | --- |
| NHL API, `api-web.nhle.com/v1` | Schedule, scores, boxscores, play-by-play | Game results, Elo, rest days, actual starting goalie | Public, no key. It returns 403 to a default urllib User-Agent. |
| MoneyPuck CSV files | `careers/gameByGame/all_teams.csv` (every team game, every season), goalie game-by-game ZIP files per season | Expected-goals form, goalie goals saved above expected | See below |
| ESPN, `site.api.espn.com/apis/site/v2/sports/hockey/nhl` | Scoreboard with `probableStartingGoalie` and a status, injuries with return dates | Starting goalies, injuries | Unofficial. ESPN tolerates use that is not abuse. |
| Daily Faceoff, `dailyfaceoff.com/starting-goalies` | Starting goalies with a status, in the page's `__NEXT_DATA__` JSON | Starting goalies | No API and no terms. The owner allows this read. |

The player catalog comes from the NHL boxscores, which name every player with an NHL ID and a position.
The NHL stats API (`api.nhle.com/stats/rest`) is not needed for v1.

### MoneyPuck terms

The MoneyPuck data page says the data is "free to use for non-commercial purposes".
It asks for credit "in all cases where you are showing anything using our data as an input".
It blocks "non-approved scraping" of any data that the page does not list.

The owner decided that hockey-model may download the listed CSV and ZIP files.
hockey-model must not fetch any other MoneyPuck URL.

The data page (`https://moneypuck.com/data.htm`, read on 2026-11-02) lists these files, among others:

- Team game-by-game, all seasons: `https://moneypuck.com/moneypuck/playerData/careers/gameByGame/all_teams.csv`.
- Goalie and skater game-by-game, one ZIP per season from 2008: `https://peter-tanner.com/moneypuck/downloads/seasonPlayersSummary/{goalies,skaters}/{YEAR}.zip`. `{YEAR}` is the first year of the season, so 2024 is 2024-25.
- Season summaries: `https://moneypuck.com/moneypuck/playerData/seasonSummary/{YEAR}/regular/{skaters,goalies,teams}.csv`.
- The players data dictionary: `https://peter-tanner.com/moneypuck/downloads/MoneyPuckDataDictionaryForPlayers.csv`.

The ZIP files are on the `peter-tanner.com` host, and the MoneyPuck page links to them.
The builder must check that the page still lists a URL before code fetches it.
Every output that uses MoneyPuck data must carry the credit text `Data from MoneyPuck.com.`

### Starting goalies

hockey-model reads the starting goalies from ESPN and from Daily Faceoff.
**Decided:** the first source that reports a goalie as confirmed wins.
When neither source has a confirmed goalie, a likely goalie from either source is used, and the result lists the goalie as unconfirmed.
When neither source names a goalie, the model estimates the starter from each goalie's recent share of starts, and the result lists the goalie as unknown.

**My call:** a goalie that ESPN lists as out cannot be the estimated starter.

### Injuries and missing regulars

**Decided:** the "missing regulars" feature (the value of the regular players who do not play in a game) is not in v1 (#8).
The backtest can get absences only from the NHL boxscores, and those absences include trades, healthy scratches, call-ups, and rest days.
A live prediction gets absences from the ESPN injury feed, which lists injuries only.
A weight fitted on one basis and applied to the other gives wrong predictions.

The ESPN feed lists only current injuries.
The per-player injury endpoint on `sports.core.api.espn.com` returned 404 on 2026-11-02, so no injury history is available.
For this reason, the backtest cannot use the ESPN feed itself.
hockey-model stores each injury read with its fetch time, so a history builds up from now on.

When about a season of injury reads exists, #8 fits the feature on those reads, so the backtest and the live prediction use the same basis.
The NHL boxscore skater rows are stored for the same reason.
In v1, the injury reads only stop an injured goalie from being the estimated starter.

**Decided (#325):** a status that this code does not name does not fail the read.
ESPN writes a status that the list does not hold, such as `Suspension`, and a rejected status failed the whole read.
A status that is not `Day-To-Day` counts as out, so an unnamed status and an unknown future status keep the injured-goalie rule safe.
The read stores the status as written, and the refresh report gets one note that names the status, the player, and the team.
A person then adds the status to the list, if it belongs there.

ESPN overwrites the morning status after a game.
Every past game that was checked (2021, 2024, and 2026) shows "Confirmed".
No source has a history of morning "likely" reads, except about 500 rows in leaguecheatr's database.

### Late runs

The owner sometimes forgets the morning run, so every time-dependent read must handle a late run.
For a game that has already started, the model uses the actual starter from the NHL boxscore.
It does not raise an error and does not stop.
The result notes what was missing, and its `made_after_start` field is true.
A score or a stat from a game in progress is never stored as final.
A morning run of `daily-prepare` that did not happen is a different case.
The report run does not do that work, because it takes about an hour and the report must finish in minutes.
It fails with exit code 1 and names the fix: run the `daily-prepare` workflow with the date of the day, then run the report again.

### Source failure

A source that is down, or that returns an unknown shape, raises a named error in the fetch.
The error names the source, the URL, and what was wrong.
The model then uses the data stored before, and the result lists that input as stale.
This is the same rule that leaguecheatr uses for ESPN, Daily Faceoff, and MoneyPuck.

### Fetch discipline

- Fetch only data that is not stored yet. A finished game is fetched once.
- Store each read with its fetch time. Do not overwrite an earlier read. The backtest needs the reads to rebuild what was known at a past time.
- Fetch the large MoneyPuck files on a slow schedule. leaguecheatr reads them once a week, which is a starting point.
- Read the ESPN scoreboard once per day per date, and keep past dates.

## Storage and refresh

**My call:** hockey-model keeps one SQLite database at a path that the caller sets.
Both consumers can share one database.
An unset path is an error that names the setting to fix.
The builder picks the setting name and the schema.

**My call:** a `hockey-model refresh` command fills the database, and the same refresh is a Python call.
The prediction call reads only stored data and makes no network request.

## The model

**My call, from the conversation:** the first model starts from a results-only Elo and adds these features:

1. MoneyPuck expected-goals form for each team, from the games before this game.
2. The starting goalie's goals saved above expected.
3. Rest days for each team.

Missing regulars are not in v1 (see "Injuries and missing regulars").

**My call:** a team with no rating, such as Vegas in 2017-18 and Seattle in 2021-22, starts at the initial rating, the same as in sportsbettr's Elo.
Both teams start before the first scored season, so the start value has several seasons to settle, and the baseline Elo uses the same rule.
Arizona and Utah are one franchise, so Utah keeps the Arizona rating.
The builder decides the feature math, the weights, the fit, and how the expected-goals interval is made.

## The backtest

The backtest is a walk-forward run: for each game, the model uses only data from before that game's start, then the game result is scored.

It reports these scores:

- Log loss.
- Brier score.
- A calibration table: the predicted chance in bins, against the share of games won.

It compares the model with two baselines:

- The home-team win rate.
- A results-only Elo.

**My call:** the results-only Elo is sportsbettr's NHL Elo (`strategies/elo.py` and its helpers in `harness/data.py`), with the market shrink removed and the margin-of-victory scaling kept.
That is the Elo that sportsbettr runs today, so a win against it is a win against the current tool.
The model's own Elo core is the same code with the same constants, so a feature cannot pass on Elo tuning alone.

**Decided:** the features are chosen only on 2015-16 to 2023-24, before any scored season.
Choosing features on the scored games would make the result look better than it is.

**Decided:** the success test is one joint test of all the features together against the Elo.
Each feature's effect can be small, and small effects can add up.
One test also avoids a spread of single-feature tests on the scored seasons.

**Decided:** the goalie-guess run can veto a feature.
On the 2022-23 and 2023-24 validation seasons, a feature is dropped when the Elo with that feature has a higher log loss than the Elo alone in the guess run.
The fit uses the training seasons (2015-16 to 2021-22) with the goalie who started, because the live model is fitted that way.
The guess run scores that same fit with the guessed goalie.
A difference of 1e-9 or less counts as no difference, so a feature with no variation is not dropped by rounding error.
A goalie feature can help when the starter is known and hurt when the guess is wrong, and a live prediction often has only a likely starter.
The validation seasons do not otherwise drop a feature, because the joint test is the test.

**Decided:** when the joint test fails, hockey-model ships the Elo alone, with every feature weight at zero.
The Elo alone is the raw Elo probability: an intercept of zero and an Elo weight of one, with no refit.
The data, the goalie starters, and the API still ship, and the report still lists each feature's effect.

**Decided:** the weights are refit for each scored season, from all seasons before it:

1. Fit through 2023-24, then score 2024-25.
2. Fit through 2024-25, then score 2025-26.

Each scored game uses only data from before it, and each scored season is scored once.

**Decided:** the model succeeds when its log loss beats the results-only Elo over the last two seasons.
**My call:** "beats" means the bootstrap confidence interval on the per-game log-loss difference is fully below zero, the same bar that sportsbettr uses to keep a strategy (`harness/bootstrap_ci.py`).
**My call:** "the last two seasons" means the last two complete regular seasons and their playoffs, 2024-25 and 2025-26. Earlier seasons are fetched to warm up the Elo, and are not scored.
**My call:** preseason games are not scored and do not update the Elo. sportsbettr excludes them for the same reason: split squads are not a real team result.

The backtest runs two ways:

1. With the goalie who actually started. This is the best case.
2. With no goalie known: the model guesses the starter from recent starts, the same as the production fallback. This shows the cost of a morning guess.

**Decided:** the actual-starter run decides success.
sportsbettr can read the goalies again close to the game, as leaguecheatr does, so the actual starter is close to what a bet sees.
The guess run is reported, and it can only veto a feature, as above.

**Decided:** the backtest uses today's MoneyPuck files for past games.
MoneyPuck possibly trained its current expected-goals model on games after the scored seasons, and no older copies of the files exist.
The leak is small, because the model rates shot quality, not team results.
The backtest report must state this limit in plain words next to every result that uses MoneyPuck data.

The backtest has no price data, so it does not say whether the model beats the market.
sportsbettr answers that question in its own backtest, after it moves to hockey-model.

**Decided:** the backtest can write one row per scored game: the game, the run, the predicted home win chance, and the result.
Another person can then score the predictions against a market line outside hockey-model.

**Decided:** a win against the Elo is necessary, but it does not show that the model makes money.
A model can beat a naive baseline and still be worse than the market line.
A results-only Elo is already close to a sportsbook line on NHL games, and several older models beat a naive baseline but lost to the line.
So the success bar here only lets the model go to sportsbettr. It does not let sportsbettr bet on it.

**Decided:** sportsbettr must not place an NHL bet from this model until its own backtest shows a positive blend weight for the model against the market.
The blend is a logistic fit of the game result on the no-vig market log-odds and the model log-odds, on walk-forward games that the fit did not see.
The model weight must be above zero, with its bootstrap confidence interval fully above zero.
A fixed shrink toward the price, such as sportsbettr's current `ALPHA`, does not satisfy this gate, because it does not measure what the model adds.
This gate belongs to sportsbettr, and phaedrus1992/sportsbettr#1419 holds it as an acceptance condition.

## Code to reuse

The builder decides what to copy and what to rewrite.
These files are the starting points:

- leaguecheatr `src/leaguecheatr/nhl_client.py`, `nhl_schedule.py`, `nhl_boxscore.py`: NHL API clients with retry.
- leaguecheatr `moneypuck.py`, `moneypuck_fetch.py`, `moneypuck_store.py`: MoneyPuck reads with strict column checks. These read only the season summary today. The game-by-game files are new.
- leaguecheatr `daily_faceoff.py`, `espn_injuries.py`, `starter_store.py`, `goalie_game_store.py`: goalie starters and injuries.
- sportsbettr `harness/nhl_plays.py`, `harness/nhl_preseason.py`, `harness/nhl_stats.py`, and the NHL day fetch in `harness/data.py` (`_fetch_nhl_day` and its callers).
- sportsbettr `strategies/elo.py` and the Elo helpers in `harness/data.py`: the baseline Elo.

sportsbettr's `strategies/nhl_moneypuck.py` is a stub: it reads MoneyPuck rows, ignores them, and returns a constant.
It has no model to port.

## Constraints

- Python 3.14, uv, and the rules in `AGENTS.md`: pinned dependencies, line length, function size, complexity, and argument count.
- hockey-model has no runtime dependencies today. Each new dependency needs a reason.
- The existing API contract in `2026-11-02-api-ux-design.md` stays valid. This design adds to it.
- The README says that hockey-model ships no production model. This project changes that, so the README must change in the same work.

## Open for the builder

- The `GameModel` protocol signature, and the names of the new types and fields.
- The database schema, the setting name for its path, and the migration approach.
- The HTTP client and the retry policy.
- The feature math, the fit, and the expected-goals interval.
- The CLI commands for a game prediction and for the backtest, and their JSON shape.
- How a source team code maps to the hockey-model team code. leaguecheatr and sportsbettr each have a map.
- How a source goalie name maps to a `Player`, through the existing `Catalog`.

## Follow-up work

These are separate projects, each with its own issue:

1. sportsbettr: replace its NHL Elo strategy with a call to hockey-model, then delete the old NHL code that hockey-model replaces (phaedrus1992/sportsbettr#1419). No NHL bet goes out until the blend-weight gate in "The backtest" passes.
2. leaguecheatr: read NHL data and goalie starters through hockey-model, then delete its copies (phaedrus1992/leaguecheatr#692).

## Calls made here

- The model never reads a market price. (Owner decision.)
- No price data is stored in hockey-model. (Owner decision.)
- The MoneyPuck listed CSV and ZIP files are allowed. Other MoneyPuck fetches are not. (Owner decision.)
- Daily Faceoff and the unofficial ESPN API are allowed. (Owner decision.)
- The first confirmed goalie, from ESPN or Daily Faceoff, wins. (Owner decision.)
- Success is a lower log loss than a results-only Elo over the last two seasons. (Owner decision.)
- Success here is necessary, not sufficient: sportsbettr must show a positive model weight in a blend with the market before it bets. (Owner decision, from review.)
- The actual-starter run decides success. The guess run can veto a feature on the validation seasons. (Owner decision.)
- One joint test of all the features against the Elo decides success. When it fails, the Elo alone ships. (Owner decision.)
- The backtest can write per-game predictions for scoring against a market line outside hockey-model. (Owner decision.)
- The backtest uses today's MoneyPuck files for past games, and the report states the leak. (Owner decision.)
- "Beats" means the block bootstrap interval on the per-game difference is fully below zero. Blocks are runs of games in start order, because games on one night share teams and goalies.
- The two scored seasons are 2024-25 and 2025-26, with playoffs. Preseason is excluded.
- The baseline Elo is sportsbettr's NHL Elo without the market shrink, and the model uses the same Elo core.
- Features are chosen on 2015-16 to 2023-24. The weights are refit before each scored season, from all earlier seasons. (Owner decision.)
- A goalie that ESPN lists as out cannot be the estimated starter.
- The "missing regulars" feature waits for injury history (#8). (Owner decision.)
- The result has a `made_after_start` field.
- A new team starts at the initial Elo rating. Utah keeps the Arizona rating.
- One shared SQLite database at a caller-set path. An unset path is an error.
- The prediction call makes no network request. A refresh fills the database.
- The candidate features are MoneyPuck expected-goals form, goalie goals saved above expected, and rest days, on top of the Elo.
- Daily Faceoff lines are out of v1.

## Built as

These points record how the build differs from, or fills in, the text above.
The code is the authority when this section and an earlier section disagree.

- **Result time.** A game result counts as known three hours after the puck drops, because the store keeps no end time. A result read has no fetch-time limit, because a backfill fetches old games late. A starter read and an injury read are known from their fetch time.
- **Player names.** The NHL boxscore writes a name as an initial and a surname, such as `C. Ingram`. ESPN and Daily Faceoff write the full name. Two names match when the first initial and the last word match. A name that matches no goalie of the team is looked up among all goalies, so a traded goalie still matches. Two matches are never resolved by a guess.
- **Daily Faceoff.** The page writes the NHL team abbreviation, so the team map checks the abbreviation against the NHL codes. A goalie that the page names with no strength is read as unknown, and the goalie resolution does not use it.
- **Source errors.** A response that parses but has a missing or wrongly typed field raises `SourceParseError`. An unknown team code raises `SourceShapeError`. A fetch failure raises `SourceError`.
- **Refresh.** A refresh runs each source in units that write nothing when they fail: a score day, a boxscore, a MoneyPuck file, or an ESPN or Daily Faceoff read. A source stops after three failed units in a row. A boxscore that failed is tried again after one day. A score day that a read covered after it ended is not read again. A game read is stored only when the game is new or changed.
- **MoneyPuck.** Rows before the 2014-15 season are not stored. The team file and the current season goalie file are read again after seven days. A finished season goalie file is read once. A row is stored only when it is new or changed.
- **Starter matching.** An ESPN or Daily Faceoff read matches the one stored game of that team whose start is within 12 hours. A read that matches no stored game is reported in the refresh notes. It is not stored and it is not an error, because ESPN also lists preseason games.
- **Bootstrap.** A block is 16 games, the busiest NHL night. The lag-1 autocorrelation of a per-game loss difference is near zero, so sportsbettr's longer autocorrelation block would add code and no width.
- **Missing inputs in the backtest.** The report counts the scored games that had each kind of missing input, so a fit on imputed zeros is visible.
- **Expected goals.** The expected goals of a side are the mean of its own MoneyPuck expected goals and the other side's expected goals against, over the last ten games. Without MoneyPuck rows the model uses the actual goals. A side with no games gets three goals and a missing-input note. The interval is the Poisson quantiles at the stated level.
- **Fit.** The fit scales each feature to unit variance and applies the L2 penalty of 1.0 to the scaled weights. The live model is fitted on the goalie who started. A fit is accepted only when no entry of the gradient of the penalized loss exceeds 1e-6, and a fit that cannot reach this raises. The saved fit carries its diagnostics: iterations, objective, final gradient, a conditioning measure, the penalty, the scaling of each column, and the stop reason.
- **Backtest result.** The first full backtest passed the joint test, and the live features are `xg_form` and `rest`. The guess run vetoed `goalie_gsax`. The report is `docs/backtest/2026-11-nhl-game-model.md`.
