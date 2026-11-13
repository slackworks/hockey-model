# Read-only consumer API (#80)

## Goal

sportsbettr (phaedrus1992/sportsbettr#1419) and leaguecheatr (phaedrus1992/leaguecheatr#692) read NHL data and predictions through one supported Python module.
They never run SQL on the store and never import an internal module.
Opening the store never creates, migrates, fits, or refreshes it.

## Non-goals

- No service. A local library is enough.
- No price data, no blend, no bet logic.
- No regulation, period, prop, futures, or in-play probability. Those are [outcome-contracts.md](outcome-contracts.md) (#83).
- No strict historical replay. #36 owns artifact and snapshot versions. Until #36 lands, every result says it is a restatement.

## Public surface

New module `src/hockey_model/consumer.py`, exported from `hockey_model/__init__.py`.

```python
def open_store(path: Path, *, fit: FitRef) -> ConsumerStore: ...

@dataclass(frozen=True, slots=True)
class FitRef:
    fit_id: int | None = None     # exactly one of the two fields is set
    label: str | None = None      # such as "live"

class ConsumerStore:               # a context manager; close() ends the read transaction
    fit: FitInfo                   # pinned when the store opens; never changes for this object
    def game(self, game_id: int) -> GameInfo: ...
    def games_on_day(self, day: date) -> tuple[GameInfo, ...]: ...      # Eastern game day
    def skater_games(self, game_id: int) -> tuple[SkaterLine, ...]: ...
    def goalie_games(self, game_id: int) -> tuple[GoalieLine, ...]: ...
    def team_xg_games(self, team: str, *, before: datetime, count: int) -> tuple[TeamXgLine, ...]: ...
    def goalie_xg_games(self, nhl_id: int, *, before: datetime, count: int) -> tuple[GoalieXgLine, ...]: ...
    def injuries(self, *, as_of: datetime) -> tuple[InjuryLine, ...]: ...
    def starter(self, game_id: int, team: str, *, as_of: datetime) -> StarterInfo: ...
    def predict(self, game_id: int, *, as_of: datetime, level: float = 0.8) -> ConsumerPrediction: ...
    def refresh_snapshot(self) -> None: ...   # end the read transaction and start a new one
```

Every returned type is a frozen dataclass in a new module `src/hockey_model/consumer_types.py`.
Every time is a UTC-aware `datetime`. A naive `datetime` argument raises `ValueError` that names the argument.

`GameInfo`: `game_id`, `season`, `game_type`, `start` (UTC), `home`, `away`, `state` (the feed state), `final` (bool), `home_score`, `away_score`, `last_period` (`REG`, `OT`, `SO`, or None), `fetched_at`.

`StarterInfo`: the `GoalieStart` of `resolve_goalie`, its `MissingInput` notes, and `confirmed` (bool). The first confirmed read wins, as in the live model. A game that started uses the boxscore starter when one exists (the leaguecheatr late-run rule).

`ConsumerPrediction`:

| Field | Meaning |
| --- | --- |
| `prediction` | the `GamePrediction` of `game_win_probability` |
| `game` | the `GameInfo` |
| `fit` | `FitInfo`: `fit_id`, `labels`, `fit_through_season`, `features`, `fit_sha256` (the same hash as the daily report), `created_at` |
| `as_of` | the argument |
| `made_after_start` | copied from `prediction` |
| `outcome` | the constant `"full_game_winner"`: the winner after overtime and the shootout |
| `reconstruction` | `"restatement"`: the inputs are today's rows, not the rows that existed at `as_of`. #36 adds `"archived"` and `"strict"`. |
| `store` | `StoreInfo`: path, schema version, newest `fetched_at` of each source table |
| `package_version` | `importlib.metadata.version("hockey-model")` |

The goal projections in `prediction` are marginal intervals. The docstring and the README MUST say that they are not a joint score distribution and that two marginals do not give a total or a margin probability.

## Opening the store

1. The path MUST exist and be a file. Else raise `StoreMissingError(path)`. Never create a file.
2. Open `sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, autocommit=False)`. Do not pass `immutable=1`: a producer can write the WAL store at the same time.
3. Run `PRAGMA query_only = ON`.
4. Read `schema_version`. A value other than `SCHEMA_VERSION` raises `StoreVersionError` with the found and the expected version and the fix (`hockey-model store upgrade`, run by the producer).
5. Check that every view in `views.sql` exists in `sqlite_master`. A missing view raises `StoreVersionError` that names the view. The consumer MUST NOT run `views.sql`.
6. Read one row to start the read transaction. With `autocommit=False` the transaction stays open, so every later read sees one snapshot of the WAL store. `refresh_snapshot` calls `rollback()` and reads one row again.
7. Resolve `fit` once (see below) and keep it for the life of the object.

Do not change `store/db.py::connect`. The producer path keeps its DDL and WAL setup.

## Fit selection

`newest_fit` serves the last saved fit, so a research fit can replace the live one (#84 note in this issue).

1. Add a table `fit_labels (label TEXT NOT NULL, fit_id INTEGER NOT NULL REFERENCES model_fits (fit_id), set_at TEXT NOT NULL)`, append-only, through `hockey-model store upgrade`. The newest row of a label wins.
2. `hockey-model fit ... --label live` saves the fit and adds the label row in one transaction. `scripts/daily-prepare.sh` passes `--label live`.
3. `hockey-model game` and `hockey-model daily` load the fit with label `live`. A store with no `live` label raises `StoreError` with the fix (`hockey-model fit ... --label live`). `newest_fit` is then dead code: delete it.
4. `FitRef(fit_id=n)` loads that fit. An unknown id raises `UnknownFitError(fit_id)`. `FitRef(label="live")` resolves the label to an id when the store opens.
5. A `FitRef` with both fields or with neither raises `ValueError`.

## Errors

New subclasses of `HockeyModelError` in `consumer_types.py`: `StoreMissingError`, `StoreVersionError`, `UnknownFitError`, `UnknownGameError`, `UnsupportedCapabilityError`.
`UnsupportedCapabilityError` is for a request that the model cannot answer at all. A missing input is never an exception: it is a `MissingInput` in the result.

## Install

The README gets an "Install for a consumer" section: `uv add "hockey-model @ git+https://github.com/slackworks/hockey-model@<tag or commit>"`.
Check whether the repository is public before the README names a token. The daily report README says the repository is public.
No consumer may use a sibling checkout path.

## Tests

New `tests/test_consumer.py`, on the seeded store:

1. Open, read, and predict with `sqlite3` writes patched to fail and the HTTP client patched to fail. Nothing fails, and the file's modification time and size do not change.
2. A missing path raises `StoreMissingError`, and no file appears.
3. `INSERT` through the consumer connection raises `sqlite3.OperationalError`.
4. Save fit A with label `live`, then a research fit B with no label. A consumer with `FitRef(label="live")` predicts with A.
5. A second connection writes new games and a new fit while a consumer is open. The open consumer sees neither until `refresh_snapshot`.
6. A postponed game, a rescheduled game, and a game with state `FINAL` and `last_period` `SO` return the right `GameInfo`.
7. `predict` after the start sets `made_after_start` and still returns a result.
8. Home and away stay in the order of the stored game for every call.
9. A store at the wrong schema version and a store with a missing view raise `StoreVersionError`.
10. Time a full slate of 16 games with `predict` on the real store, and write the time in the pull request. Add a batch call only if that time is too slow for sportsbettr.

## Prerequisites and order

1. Fit labels (this design) and the `game`/`daily` switch. One pull request.
2. `consumer.py` with reads and `predict`. One pull request.
3. README and the install example. Same pull request as 2.

#65, #66, #67, #69, and #72 are data fixes that the issue lists as needed before a cutover. They do not block the API itself.

## Calls made

- One open read transaction for the life of the object, so a slate never mixes two store states. `refresh_snapshot` is explicit.
- Fit labels in a new table, not a column on `model_fits`, so a label can move to a new fit and keep its history.
- `reconstruction` is always `"restatement"` until #36, so no consumer can read a result as a strict replay.
