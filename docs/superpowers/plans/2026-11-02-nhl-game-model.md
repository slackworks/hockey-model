# NHL Game Win Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, inline in the current session.
> The owner's rules override superpowers:subagent-driven-development for this repo.
> Execute through `dev-sprint` and `nbl-dev:ship-issue` for issue #7.
> Stop after the last task and return to `ship-issue` Step 6.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** hockey-model fetches its own NHL data, predicts the win chance of each side of an NHL game, and backtests that prediction against game results and a results-only Elo.

**Architecture:** Four layers, each in its own package under `src/hockey_model/`.
`sources/` fetches and parses one external source per module and does no storage.
`store/` owns the SQLite schema, the writes, the as-of reads, and `refresh`.
`game_model/` holds the Elo, the features, the logistic fit, and the `GameModel` implementation.
`backtest/` holds the metrics and the walk-forward runner.
The public `Game` types and `game_win_probability()` live in `src/hockey_model/game.py`.

**Tech Stack:** Python 3.14 standard library only (`urllib.request`, `sqlite3`, `csv`, `zipfile`, `json`, `random`, `math`).
pytest and hypothesis for tests.

**Spec:** `docs/superpowers/specs/2026-11-02-nhl-game-model-design.md` (read it with this plan).

## Global Constraints

- Python 3.14 and uv. `uv run python3`, never bare `python3`.
- No new runtime dependency. `pyproject.toml` `dependencies = []` stays empty.
- Line length 100, function complexity 8 or less, 5 positional arguments or less, 100 lines per function or less.
- Absolute imports only (`from hockey_model.store.db import connect`).
- Per-module coverage floor: `uv run python3 scripts/check_per_module_coverage.py` must pass after each task.
- Tests make no network request. Each source test reads a fixture file under `tests/fixtures/<source>/`.
- Every error names the operation, the input, and the fix (see `types.py` messages).
- No price data. No Kalshi, Odds API, sportsbook, or ESPN odds field is parsed or stored.
- MoneyPuck: fetch only the URLs in `sources/moneypuck.py` `ALLOWED_URLS`. Every output that uses MoneyPuck data carries `MONEYPUCK_CREDIT = "Data from MoneyPuck.com."`.
- Every stored source read keeps its `fetched_at`. A later read never overwrites an earlier read.
- A game whose state is not final is never stored as final.
- All times are timezone-aware UTC `datetime`. As-of comparisons use the game start time, never a calendar date.
- The prediction call makes no network request.
- Database path: environment variable `HOCKEY_MODEL_DB`. Unset is an error that names the variable.
- HTTP User-Agent: `hockey-model/<package version>`. `api-web.nhle.com` returns 403 to the default urllib agent.

## Review Focus

1. Team codes across sources and seasons: ESPN writes `LA`, `NJ`, `SJ`, `TB`; Arizona (`ARI`) became Utah (`UTA`) in 2024-25. An unknown code raises `SourceShapeError` naming the code. Pinned in Task 4.
2. A postponed game, or a game in progress at refresh time: the row stays non-final, and a later refresh finishes it. Pinned in Task 5.
3. A late west-coast game starts after midnight UTC. As-of reads use the start time, so the game before it on the same calendar day is still visible and the game itself is not. Pinned in Task 10.
4. A goalie name from ESPN or Daily Faceoff that matches no goalie, or two goalies, on that team: the prediction does not crash, and it reports `GOALIE_UNKNOWN` with the name in the note. Pinned in Task 10.
5. A refresh that fails halfway through one source: that source writes nothing, the other sources still write, and the refresh exits non-zero with every failure listed. Pinned in Task 9.

---

### Task 1: Public game types and `game_win_probability`

**Files:**
- Create: `src/hockey_model/game.py`
- Modify: `src/hockey_model/models.py` (add `GameModel`)
- Modify: `src/hockey_model/__init__.py` (export the new names)
- Test: `tests/test_game.py`

**Interfaces:**
- Produces:
  - `class StarterStatus(StrEnum)`: `CONFIRMED = "confirmed"`, `LIKELY = "likely"`, `UNKNOWN = "unknown"`.
  - `class StarterSource(StrEnum)`: `ESPN = "espn"`, `DAILY_FACEOFF = "daily_faceoff"`, `NHL_BOXSCORE = "nhl_boxscore"`, `MODEL_GUESS = "model_guess"`.
  - `@dataclass(frozen=True, slots=True) class GoalieStart`: `player: Player`, `status: StarterStatus`, `source: StarterSource`, `read_at: datetime`.
  - `@dataclass(frozen=True, slots=True) class Game`: `game_id: int`, `home: str`, `away: str`, `start: datetime`, `home_goalie: GoalieStart | None = None`, `away_goalie: GoalieStart | None = None`.
  - `class MissingKind(StrEnum)`: `GOALIE_UNCONFIRMED`, `GOALIE_UNKNOWN`, `XG_FORM`, `GOALIE_FORM`, `STALE_SOURCE`, `LATE_RUN` (values are the lowercase names).
  - `@dataclass(frozen=True, slots=True) class MissingInput`: `kind: MissingKind`, `team: str | None`, `note: str`.
  - `@dataclass(frozen=True, slots=True) class GamePrediction`: `home_win: float`, `away_win: float`, `home_goals: Projection`, `away_goals: Projection`, `missing: tuple[MissingInput, ...]`, `credits: tuple[str, ...]`, `made_after_start: bool = False`.
  - `GameModel` protocol in `models.py`: `def predict(self, game: Game, as_of: datetime, level: float) -> GamePrediction`.
  - `def game_win_probability(game: Game, model: GameModel, *, as_of: datetime | None = None, level: float = 0.8) -> GamePrediction`. It sets `made_after_start = as_of >= game.start` on the result with `dataclasses.replace`, so no model can get it wrong.

- [ ] **Step 1: Write the failing tests** in `tests/test_game.py`:
  - `test_game_rejects_naive_start`: `Game(1, "TOR", "MTL", datetime(2026, 1, 1))` raises `ValueError` with "timezone".
  - `test_game_rejects_same_team`: `home == away` raises `ValueError`.
  - `test_game_rejects_nonpositive_id`: `game_id=0` raises `ValueError`.
  - `test_game_win_probability_returns_model_result`: a stub model returning `0.6, 0.4` gives `result.home_win == 0.6`.
  - `test_made_after_start_flag`: `as_of` one second before `game.start` gives `False`; `as_of == game.start` and one second after give `True`, even when the stub model returns `True` or `False` the other way.
  - `test_game_win_probability_defaults_as_of_to_now`: the stub records `as_of`; it is aware UTC and within 5 seconds of `datetime.now(UTC)`.
  - `test_game_win_probability_rejects_bad_sum`: stub returns `0.6, 0.5`; raises `ProbabilityError` with "sum to 1.0".
  - `test_game_win_probability_rejects_nan`: stub returns `nan`; raises `ProbabilityError`.
  - `test_game_win_probability_rejects_bad_level`: `level=1.0` raises `ValueError`.
  - `test_game_win_probability_rejects_naive_as_of`: raises `ValueError`.
- [ ] **Step 2: Run** `uv run pytest tests/test_game.py -v`. Expected: FAIL, `ModuleNotFoundError: hockey_model.game`.
- [ ] **Step 3: Implement** the types and `game_win_probability` in `src/hockey_model/game.py`, and `GameModel` in `models.py`. Reuse the probability checks of `matchup()`: move them into one private helper `_check_win_pair(values, operation)` in `matchup.py` and call it from both places.
- [ ] **Step 4: Run** `uv run pytest tests/test_game.py tests/test_matchup.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): add game input and game win probability`.

### Task 2: Database connection and schema

**Files:**
- Create: `src/hockey_model/store/__init__.py`, `src/hockey_model/store/db.py`, `src/hockey_model/store/schema.sql`
- Test: `tests/store/test_db.py`

**Interfaces:**
- Produces:
  - `DB_ENV = "HOCKEY_MODEL_DB"`, `SCHEMA_VERSION = 1`.
  - `class StoreError(HockeyModelError)`.
  - `def db_path_from_env(environ: Mapping[str, str]) -> Path`: raises `StoreError` naming `HOCKEY_MODEL_DB` when unset or empty.
  - `def connect(path: Path) -> sqlite3.Connection`: creates the parent directory, sets `foreign_keys=ON` and `journal_mode=WAL`, applies `schema.sql` when the file is new, and raises `StoreError` when the stored `schema_version` differs from `SCHEMA_VERSION`.
  - Tables (all append-only, each with `fetched_at TEXT NOT NULL` as ISO UTC):
    - `schema_version(version INTEGER NOT NULL)`.
    - `source_reads(source TEXT, url TEXT, fetched_at TEXT, ok INTEGER, error TEXT)`.
    - `games(game_id INTEGER, season INTEGER, game_type INTEGER, start_utc TEXT, home TEXT, away TEXT, state TEXT, home_score INTEGER, away_score INTEGER, last_period TEXT, fetched_at TEXT, PRIMARY KEY (game_id, fetched_at))`.
    - `players(nhl_id INTEGER, full_name TEXT, position TEXT, team TEXT, fetched_at TEXT, PRIMARY KEY (nhl_id, fetched_at))`.
    - `skater_games(game_id INTEGER, team TEXT, nhl_id INTEGER, toi_seconds INTEGER, goals INTEGER, assists INTEGER, fetched_at TEXT, PRIMARY KEY (game_id, nhl_id))`.
    - `goalie_games(game_id INTEGER, team TEXT, nhl_id INTEGER, toi_seconds INTEGER, started INTEGER, fetched_at TEXT, PRIMARY KEY (game_id, nhl_id))`.
    - `mp_team_games(game_id INTEGER, team TEXT, game_date TEXT, xg_for REAL, xg_against REAL, goals_for INTEGER, goals_against INTEGER, fetched_at TEXT, PRIMARY KEY (game_id, team, fetched_at))`.
    - `mp_goalie_games(game_id INTEGER, nhl_id INTEGER, team TEXT, xg_against REAL, goals_against INTEGER, fetched_at TEXT, PRIMARY KEY (game_id, nhl_id, fetched_at))`.
    - `starter_reads(game_id INTEGER, team TEXT, source TEXT, goalie_name TEXT, nhl_id INTEGER, status TEXT, fetched_at TEXT)`.
    - `injury_reads(team TEXT, player_name TEXT, nhl_id INTEGER, status TEXT, return_date TEXT, fetched_at TEXT)`.
    - `model_fits(fit_id INTEGER PRIMARY KEY, fit_through_season INTEGER, features TEXT, weights TEXT, created_at TEXT)`.
  - `skater_games` and `goalie_games` key on the game only, because they are written once from a final boxscore.

- [ ] **Step 1: Write the failing tests**:
  - `test_db_path_from_env_unset`: `{}` raises `StoreError`; the message contains `HOCKEY_MODEL_DB` and "set".
  - `test_db_path_from_env_empty`: `{"HOCKEY_MODEL_DB": ""}` raises `StoreError`.
  - `test_connect_creates_schema(tmp_path)`: every table above exists; `schema_version` holds `1`.
  - `test_connect_reopens_existing(tmp_path)`: two connects; the second does not fail and keeps a row written by the first.
  - `test_connect_rejects_other_version(tmp_path)`: set `schema_version` to 99; reconnect raises `StoreError` with "99".
- [ ] **Step 2: Run** `uv run pytest tests/store/test_db.py -v`. Expected: FAIL on import.
- [ ] **Step 3: Implement** `db.py` and `schema.sql`. Load `schema.sql` with `importlib.resources`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(store): add sqlite store and schema`.

### Task 3: Polite HTTP client

**Files:**
- Create: `src/hockey_model/sources/__init__.py`, `src/hockey_model/sources/http.py`
- Test: `tests/sources/test_http.py`

**Interfaces:**
- Produces:
  - `class SourceError(HockeyModelError)`: attributes `source: str`, `url: str`, `reason: str`. Message: `fetch {source} {url}: {reason}; the stored data stays in use`.
  - `class SourceShapeError(SourceError)`: a response that parsed but has an unknown shape.
  - `@dataclass(frozen=True) class HostPolicy`: `min_interval_s: float`, `max_retries: int`.
  - `HOST_POLICIES: Mapping[str, HostPolicy]`: `api-web.nhle.com` 0.5 s, `site.api.espn.com` 2.0 s, `www.dailyfaceoff.com` 5.0 s, `moneypuck.com` 5.0 s, `peter-tanner.com` 5.0 s. All `max_retries=3`.
  - `class HttpClient`: `__init__(self, opener: Callable[[urllib.request.Request, float], bytes] | None = None, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)`; `def get(self, source: str, url: str) -> bytes`.
- `get` waits so that two requests to one host start at least `min_interval_s` apart. It retries on HTTP 429, 5xx, and `URLError`, with backoff `min(2**attempt, 30)` seconds or `Retry-After` when present. It raises `SourceError` on a 4xx other than 429, on a host not in `HOST_POLICIES`, and after the last retry.

- [ ] **Step 1: Write the failing tests** (inject `opener`, `clock`, `sleep`; no real network):
  - `test_get_sends_user_agent`: the opener sees `User-Agent` starting with `hockey-model/`.
  - `test_get_spaces_requests_per_host`: two calls to `api-web.nhle.com` at clock 0.0 and 0.1 cause `sleep(0.4)`.
  - `test_get_retries_429_with_retry_after`: 429 with `Retry-After: 7`, then 200; `sleep(7.0)` is called and the body returns.
  - `test_get_raises_after_last_retry`: three 503 responses raise `SourceError`; `reason` contains "503".
  - `test_get_raises_on_404_without_retry`: one opener call only.
  - `test_get_rejects_unknown_host`: `https://example.com/x` raises `SourceError` with "no host policy".
- [ ] **Step 2: Run** `uv run pytest tests/sources/test_http.py -v`. Expected: FAIL on import.
- [ ] **Step 3: Implement** `http.py`. The default opener is `urllib.request.urlopen(request, timeout=30).read()`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sources): add polite http client`.

### Task 4: Team codes

**Files:**
- Create: `src/hockey_model/sources/teams.py`
- Test: `tests/sources/test_teams.py`

**Interfaces:**
- Produces:
  - `NHL_TEAM_CODES: frozenset[str]`: the 32 codes of 2026-27 (copy from leaguecheatr `src/leaguecheatr/nhl_schedule.py`), plus the historical `ARI`.
  - `def franchise(code: str) -> str`: maps `ARI` to `UTA`; other codes map to themselves. Elo, form, and rest use the franchise code.
  - `def from_espn(code: str) -> str`: maps `LA`→`LAK`, `NJ`→`NJD`, `SJ`→`SJS`, `TB`→`TBL`, then checks membership.
  - `def from_daily_faceoff(name: str) -> str`: maps the team name that Daily Faceoff writes to the NHL code. Build the table from the fixture of Task 8.
  - `def from_moneypuck(code: str) -> str`: maps the team code that MoneyPuck writes to the NHL code. Build the table from the fixture of Task 6, because the MoneyPuck codes are not confirmed to equal the NHL codes.
  - Each function raises `SourceShapeError` with the unknown value and "add it to sources/teams.py".

- [ ] **Step 1: Write the failing tests**: `franchise("ARI") == "UTA"`, `franchise("TOR") == "TOR"`, `from_espn("TB") == "TBL"`, `from_espn("UTAH")` raises `SourceShapeError` naming `"UTAH"`, and a hypothesis test that `from_espn(c) == c` for every `c` in `NHL_TEAM_CODES - {"LAK", "NJD", "SJS", "TBL", "ARI"}`.
- [ ] **Step 2: Run** `uv run pytest tests/sources/test_teams.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `teams.py`. `from_moneypuck` waits for Task 6 and `from_daily_faceoff` waits for Task 8.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sources): add nhl team code maps`.

### Task 5: NHL API source and its store writes

**Files:**
- Create: `src/hockey_model/sources/nhl.py`, `src/hockey_model/store/nhl_writes.py`
- Create fixtures: `tests/fixtures/nhl/score_<date>.json` (one day with a final, a regulation game, an OT game, a shootout game, a preseason game, and one postponed or future game), `tests/fixtures/nhl/boxscore_<id>.json` (one final game)
- Test: `tests/sources/test_nhl.py`, `tests/store/test_nhl_writes.py`

**Interfaces:**
- Consumes: `HttpClient.get` (Task 3), `connect` (Task 2), `franchise` and `NHL_TEAM_CODES` (Task 4).
- Produces:
  - `NHL_BASE = "https://api-web.nhle.com/v1"`; `GAME_TYPES_KEPT = frozenset({2, 3})` (regular season, playoffs).
  - `@dataclass(frozen=True) class GameRow`: `game_id: int`, `season: int`, `game_type: int`, `start: datetime`, `home: str`, `away: str`, `state: str`, `home_score: int | None`, `away_score: int | None`, `last_period: str | None`; property `is_final -> bool` (state in `{"FINAL", "OFF"}`).
  - `def parse_score_day(payload: object) -> list[GameRow]`: drops game types outside `GAME_TYPES_KEPT`.
  - `@dataclass(frozen=True) class BoxscoreRows`: `players: list[tuple[int, str, str, str]]` (nhl_id, full name, position, team), `skaters: list[tuple[int, str, int, int, int, int]]`, `goalies: list[tuple[int, str, int, int, int]]` (matching the table columns of Task 2 minus `fetched_at`).
  - `def parse_boxscore(payload: object) -> BoxscoreRows`: reads `playerByGameStats.{home,away}Team.{forwards,defense,goalies}`; `toi` is `MM:SS`; a goalie's `started` comes from the `starter` flag.
  - `def fetch_score_day(client: HttpClient, day: date) -> list[GameRow]` (`/score/{YYYY-MM-DD}`), `def fetch_boxscore(client: HttpClient, game_id: int) -> BoxscoreRows` (`/gamecenter/{id}/boxscore`).
  - `def write_games(conn, rows: Sequence[GameRow], fetched_at: datetime) -> int`; `def write_boxscore(conn, game_id: int, rows: BoxscoreRows, fetched_at: datetime) -> None`; `def games_missing_boxscore(conn) -> list[int]` (final games with no `goalie_games` rows).
- Reuse: leaguecheatr `nhl_boxscore.py` (field checks and `MM:SS` parse) and sportsbettr `harness/data.py` `parse_nhl_day` (score day shape).

- [ ] **Step 1: Capture fixtures** with one request each through `HttpClient` from an `uv run python3 -c` one-liner. Pick a date in 2025-26 with an OT and a shootout game. Strip nothing.
- [ ] **Step 2: Write the failing tests**:
  - `test_parse_score_day_drops_preseason`, `test_parse_score_day_keeps_only_types_2_and_3` (copies of one fixture game with `gameType` 1, 4, 9, and 19 are dropped; 2 and 3 are kept), `test_parse_score_day_reads_final_scores`, `test_parse_score_day_marks_ot_and_so` (`last_period in {"OT", "SO"}`), `test_parse_score_day_future_game_is_not_final` (scores `None`, `is_final is False`).
  - `test_parse_score_day_rejects_missing_id`: delete `id` from a copy; raises `SourceShapeError` naming `id`.
  - `test_parse_boxscore_one_starter_per_team`, `test_parse_boxscore_toi_seconds` (`"18:30"` → 1110), `test_parse_boxscore_rejects_bad_toi` (`"1830"` → `SourceShapeError`).
  - `test_write_games_keeps_both_reads`: write a non-final row, then the final row for the same game; both rows remain.
  - `test_games_missing_boxscore`: lists a final game with no goalie rows and not a non-final game.
- [ ] **Step 3: Run** the tests. Expected: FAIL.
- [ ] **Step 4: Implement** `nhl.py` and `nhl_writes.py`. Raise `SourceShapeError` for each missing or wrongly typed field.
- [ ] **Step 5: Run** the tests. Expected: PASS.
- [ ] **Step 6: Commit** `feat(sources): add nhl api score and boxscore reads`.

### Task 6: MoneyPuck source and its store writes

**Files:**
- Create: `src/hockey_model/sources/moneypuck.py`, `src/hockey_model/store/moneypuck_writes.py`
- Create fixtures: `tests/fixtures/moneypuck/all_teams_head.csv` (the header and about 40 rows of `all_teams.csv`), `tests/fixtures/moneypuck/goalies_2024_head.zip` (a ZIP that holds the header and about 40 rows of the 2024 goalie file)
- Test: `tests/sources/test_moneypuck.py`, `tests/store/test_moneypuck_writes.py`

**Interfaces:**
- Produces:
  - `MONEYPUCK_CREDIT = "Data from MoneyPuck.com."`.
  - `TEAM_GAMES_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/all_teams.csv"`.
  - `def goalie_games_url(season_start_year: int) -> str`: `https://peter-tanner.com/moneypuck/downloads/seasonPlayersSummary/goalies/{year}.zip`.
  - `ALLOWED_URLS`: a check function `def check_allowed(url: str) -> None` that raises `SourceError` for any other URL.
  - `REFRESH_AFTER = timedelta(days=7)`.
  - `def parse_team_games(text: str) -> list[tuple[int, str, str, float, float, int, int]]` and `def parse_goalie_games(data: bytes) -> list[tuple[int, int, str, float, int]]`, each matching its table of Task 2 minus `fetched_at`. Only the `situation == "all"` rows are kept.
  - `def write_team_games(conn, rows, fetched_at) -> int`, `def write_goalie_games(conn, rows, fetched_at) -> int`, `def last_moneypuck_fetch(conn, url: str) -> datetime | None`.
- Reuse: leaguecheatr `moneypuck.py` (strict column-set check, number parse, error shape).

- [ ] **Step 1: Capture fixtures.** Download each file once, keep the header and the first 40 data rows, and record the download date in a comment row of `tests/fixtures/moneypuck/README.md`. Read the column names from the real header and from `MoneyPuckDataDictionaryForPlayers.csv`. Pin the exact column names as constants in `moneypuck.py`.
- [ ] **Step 2: Write the failing tests**:
  - `test_check_allowed_rejects_other_moneypuck_url`: `https://moneypuck.com/teams.htm` raises `SourceError`.
  - `test_parse_team_games_keeps_all_situation_only`.
  - `test_parse_team_games_maps_team_codes`: every fixture team code goes through `from_moneypuck` (add it to `teams.py` in this task); an unknown code raises `SourceShapeError`.
  - `test_moneypuck_game_ids_join_nhl`: each fixture `gameId` maps to the 10-digit NHL game ID of the same game (season, type, number). If MoneyPuck writes another form, add `def nhl_game_id(raw: str) -> int` to `moneypuck.py` and test it here.
  - `test_parse_team_games_rejects_missing_column`: drop the `xGoalsFor` column (or its pinned name); raises `SourceShapeError` naming it.
  - `test_parse_team_games_rejects_non_number`: raises `SourceShapeError` naming the line.
  - `test_parse_goalie_games_reads_zip`, `test_parse_goalie_games_rejects_zip_with_two_files`.
- [ ] **Step 3: Run** the tests. Expected: FAIL.
- [ ] **Step 4: Implement** both modules.
- [ ] **Step 5: Run** the tests. Expected: PASS.
- [ ] **Step 6: Commit** `feat(sources): add moneypuck game-by-game reads`.

### Task 7: ESPN source and its store writes

**Files:**
- Create: `src/hockey_model/sources/espn.py`, `src/hockey_model/store/espn_writes.py`
- Create fixtures: `tests/fixtures/espn/scoreboard_upcoming.json` (games not started, with `Confirmed` and `Expected`), `tests/fixtures/espn/injuries.json`
- Test: `tests/sources/test_espn.py`, `tests/store/test_espn_writes.py`

**Interfaces:**
- Produces:
  - `ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"`.
  - `@dataclass(frozen=True) class StarterRead`: `start: datetime`, `team: str`, `goalie_name: str`, `status: StarterStatus`, `source: StarterSource`.
  - `def parse_scoreboard_starters(payload: object) -> list[StarterRead]`: reads `events[].competitions[0].competitors[].probables[]` where `name == "probableStartingGoalie"`. Maps `Confirmed`→`CONFIRMED`, `Expected`→`LIKELY`. Another status raises `SourceShapeError` naming it. A game whose `status.type.state` is not `pre` is skipped, because ESPN overwrites the status after the start.
  - `OUT_STATUSES = frozenset({"Out", "Injured Reserve", "Long Term Injured Reserve"})`; `@dataclass(frozen=True) class InjuryRead`: `team: str`, `player_name: str`, `status: str`, `return_date: date | None`; property `is_out -> bool`.
  - `def parse_injuries(payload: object) -> list[InjuryRead]`. A status that is not in `OUT_STATUSES` and not `"Day-To-Day"` raises `SourceShapeError` naming it.
  - `def fetch_scoreboard_starters(client, day: date) -> list[StarterRead]` (`/scoreboard?dates=YYYYMMDD`), `def fetch_injuries(client) -> list[InjuryRead]` (`/injuries`).
  - `def write_starter_reads(conn, game_ids: Mapping[tuple[datetime, str], int], reads, fetched_at) -> int`: matches each read to an NHL game by start time and team; an unmatched read raises `SourceShapeError`. `def write_injury_reads(conn, reads, fetched_at) -> int`.
  - `nhl_id` in both tables is filled later by Task 10. It stays `NULL` at write time.

- [ ] **Step 1: Capture fixtures** with one request each, on a day with upcoming games.
- [ ] **Step 2: Write the failing tests**: status mapping, the skip of a started game, the unknown-status error, `from_espn` code mapping on `TB`, injury `is_out` for each status in `OUT_STATUSES`, `Day-To-Day` is not out, and a missing `athlete` key raises `SourceShapeError`.
- [ ] **Step 3: Run** the tests. Expected: FAIL.
- [ ] **Step 4: Implement** both modules. Read no `odds` or `pickcenter` field.
- [ ] **Step 5: Run** the tests. Expected: PASS.
- [ ] **Step 6: Commit** `feat(sources): add espn starter and injury reads`.

### Task 8: Daily Faceoff source

**Files:**
- Create: `src/hockey_model/sources/daily_faceoff.py`
- Modify: `src/hockey_model/sources/teams.py` (add `from_daily_faceoff`)
- Create fixture: `tests/fixtures/daily_faceoff/starting_goalies.html`
- Test: `tests/sources/test_daily_faceoff.py`

**Interfaces:**
- Consumes: `StarterRead` (Task 7), `write_starter_reads` (Task 7).
- Produces: `STARTERS_URL = "https://www.dailyfaceoff.com/starting-goalies"`; `def parse_starters(html: str) -> list[StarterRead]` with `source=StarterSource.DAILY_FACEOFF`; `def fetch_starters(client) -> list[StarterRead]`.
- Status map, from leaguecheatr `daily_faceoff.py`: `Confirmed`→`CONFIRMED`, `Likely`→`LIKELY`, none→`UNKNOWN`. Another value raises `SourceShapeError`.
- Reuse: leaguecheatr `daily_faceoff.py` (`__NEXT_DATA__` JSON extract, `props.pageProps.data`).

- [ ] **Step 1: Capture the fixture** with one request.
- [ ] **Step 2: Write the failing tests**: the three status values, an unknown status, a page with no `__NEXT_DATA__` script (raises `SourceShapeError` with "__NEXT_DATA__"), and `from_daily_faceoff` for every team name in the fixture.
- [ ] **Step 3: Run** the tests. Expected: FAIL.
- [ ] **Step 4: Implement** the parser and the team-name table.
- [ ] **Step 5: Run** the tests. Expected: PASS.
- [ ] **Step 6: Commit** `feat(sources): add daily faceoff starter reads`.

### Task 9: Refresh and the `refresh` command

**Files:**
- Create: `src/hockey_model/store/refresh.py`
- Modify: `src/hockey_model/cli.py` (add `refresh`)
- Test: `tests/store/test_refresh.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: every fetch and write function of Tasks 5 to 8.
- Produces:
  - `@dataclass(frozen=True) class RefreshReport`: `written: Mapping[str, int]`, `errors: tuple[SourceError, ...]`.
  - `def refresh(conn, client: HttpClient, *, now: datetime, backfill_from: date | None = None) -> RefreshReport`.
- Order: NHL score days (from the newest stored day, or `backfill_from`, through `now + 2 days`), then NHL boxscores for `games_missing_boxscore`, then MoneyPuck (only when `last_moneypuck_fetch` is older than `REFRESH_AFTER`; goalie files for each season from 2014 that has no rows, plus the current season), then ESPN starters for today and tomorrow (UTC), then ESPN injuries, then Daily Faceoff.
- Each source writes in one transaction (`with conn:`). A `SourceError` rolls back that source only, is recorded in `source_reads` with `ok=0`, and goes into `errors`. Every source runs.
- CLI: `hockey-model refresh [--backfill-from YYYY-MM-DD]` opens `connect(db_path_from_env(os.environ))`, prints `RefreshReport` as JSON, and exits 1 when `errors` is not empty. It prints `MONEYPUCK_CREDIT` in the JSON field `credits` when MoneyPuck rows were written.

- [ ] **Step 1: Write the failing tests** with a fake `HttpClient` that serves the fixtures of Tasks 5 to 8 by URL:
  - `test_refresh_writes_every_source`.
  - `test_refresh_isolates_source_failure`: ESPN raises `SourceShapeError` halfway through; ESPN writes zero rows, NHL rows exist, `errors` has one entry, and `source_reads` has an `ok=0` row naming ESPN.
  - `test_refresh_skips_fresh_moneypuck`: a MoneyPuck read 3 days old means no MoneyPuck request.
  - `test_refresh_is_incremental`: a second run requests no score day older than the newest stored day and no boxscore already stored.
  - `test_cli_refresh_exits_one_on_error` and `test_cli_refresh_requires_db_env`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** `refresh.py` and the CLI command. `refresh` must stay under complexity 8: one small function per source, called from a list.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(store): add refresh across all sources`.

### Task 10: As-of reads, player matching, and goalie resolution

**Files:**
- Create: `src/hockey_model/store/reads.py`, `src/hockey_model/game_model/__init__.py`, `src/hockey_model/game_model/goalies.py`
- Test: `tests/store/test_reads.py`, `tests/game_model/test_goalies.py`

**Interfaces:**
- Consumes: the tables of Task 2, `Catalog` (`identity.py`), the types of Task 1.
- Produces in `reads.py` (every read takes `before: datetime` and returns only rows whose game start is earlier than `before` and whose `fetched_at` is earlier than `before`):
  - `def final_games_before(conn, before: datetime) -> list[GameRow]`: the newest final read of each game.
  - `def team_games_before(conn, team: str, before: datetime, limit: int) -> list[GameRow]`: by franchise code, newest first.
  - `def goalie_starts_before(conn, team: str, before: datetime, limit: int) -> list[tuple[int, int]]` (game_id, nhl_id of the starter).
  - `def mp_team_form_before(conn, team: str, before: datetime, limit: int) -> list[tuple[float, float]]` (xg_for, xg_against).
  - `def mp_goalie_form_before(conn, nhl_id: int, before: datetime, limit: int) -> list[tuple[float, int]]`.
  - `@dataclass(frozen=True) class StarterReadRow`: `source: StarterSource`, `goalie_name: str`, `nhl_id: int | None`, `status: StarterStatus`, `fetched_at: datetime`.
  - `def starter_reads_before(conn, game_id: int, team: str, before: datetime) -> list[StarterReadRow]` (oldest first) and `def injury_reads_before(conn, before: datetime) -> list[InjuryRead]` (newest read set only).
  - `def player_catalog(conn) -> Catalog`: one `Player` per `nhl_id` from the newest `players` row. `canonical_id = f"nhl:{nhl_id}"`, `eligible` from position (`C`→C, `L`/`R`→W, `D`→D, `G`→G).
- Produces in `goalies.py`:
  - `STARTER_WINDOW = 10`.
  - `def resolve_goalie(conn, game: Game, team: str, as_of: datetime, *, use_reads: bool) -> tuple[GoalieStart | None, tuple[MissingInput, ...]]`.
- Resolution order:
  1. A `GoalieStart` already on `game` wins.
  2. When `as_of >= game.start` and a boxscore exists, use its starter with `source=NHL_BOXSCORE` and add `MissingInput(LATE_RUN, team, ...)`.
  3. When `use_reads`, the earliest read with status `CONFIRMED` from ESPN or Daily Faceoff wins.
  4. Else, the newest `LIKELY` read, with `GOALIE_UNCONFIRMED`.
  5. Else, the goalie with the most starts in the last `STARTER_WINDOW` team games, skipping a goalie whose newest injury read `is_out`, with `source=MODEL_GUESS`, `status=UNKNOWN`, and `GOALIE_UNKNOWN`.
  6. A read name is matched with `Catalog.resolve` over that team's goalies only. No match or two matches gives `GOALIE_UNKNOWN` with the name in the note, and resolution goes on to the next step.

- [ ] **Step 1: Write the failing tests** on an in-memory database seeded by small helpers:
  - `test_reads_hide_game_at_as_of`: a game that starts exactly at `before` is not returned.
  - `test_reads_late_west_coast_game`: games at 2026-01-10T03:30Z and 2026-01-10T00:00Z; `before=2026-01-10T03:30Z` returns only the second.
  - `test_reads_hide_rows_fetched_after_as_of`.
  - `test_team_games_follow_franchise`: a stored `ARI` game is returned for `UTA`.
  - `test_resolve_first_confirmed_wins`: ESPN confirmed at 15:00, Daily Faceoff confirmed at 14:00 for another goalie; Daily Faceoff wins.
  - `test_resolve_likely_reports_unconfirmed`.
  - `test_resolve_guess_skips_injured_goalie`.
  - `test_resolve_unknown_name_reports_name`: a read for "Nobody Here"; the note contains "Nobody Here" and the guess is used.
  - `test_resolve_late_run_uses_boxscore`.
  - `test_resolve_without_reads_ignores_reads`: `use_reads=False` gives the guess even when a confirmed read exists.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** `reads.py` and `goalies.py`. Each read is one SQL query.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(model): add as-of reads and goalie resolution`.

### Task 11: Elo and features

**Files:**
- Create: `src/hockey_model/game_model/elo.py`, `src/hockey_model/game_model/features.py`
- Test: `tests/game_model/test_elo.py`, `tests/game_model/test_features.py`

**Interfaces:**
- Consumes: `reads.py` and `resolve_goalie` (Task 10).
- Produces in `elo.py` (ported from sportsbettr `harness/data.py` `apply_elo_updates`, `_elo_expected`, `_elo_margin_factor`; NHL constants):
  - `ELO_INITIAL = 1500.0`, `ELO_K = 8.0`, `ELO_HOME_ADV = 28.0`.
  - `def elo_expected(home: float, away: float) -> float`.
  - `class EloBook`: `def update(self, game: GameRow) -> None` (franchise codes; `delta = K * (actual - expected) * log(1 + |margin|)`; a game that is not final raises `ValueError`), `def rating(self, team: str) -> float`, `def home_win_chance(self, home: str, away: str) -> float`.
  - `def replay(games: Iterable[GameRow]) -> EloBook` (games in start order).
- Produces in `features.py`:
  - `FEATURES = ("xg_form", "goalie_gsax", "rest")`. Missing regulars wait for #8; the skater rows of Task 5 stay stored for it.
  - `FORM_WINDOW = 10`, `GOALIE_WINDOW = 20`, `REST_CAP_DAYS = 4`.
  - `@dataclass(frozen=True) class FeatureRow`: `game_id: int`, `elo_logit: float`, `values: Mapping[str, float]`, `missing: tuple[MissingInput, ...]`, `uses_moneypuck: bool`.
  - `def build_features(conn, game: Game, as_of: datetime, elo: EloBook, *, goalie_mode: GoalieMode) -> FeatureRow`, with `class GoalieMode(StrEnum)`: `ACTUAL = "actual"` (the boxscore starter, backtest only), `READS = "reads"` (live: `resolve_goalie(use_reads=True)`), `GUESS = "guess"` (`resolve_goalie(use_reads=False)`).
- Feature definitions (each is home minus away; `0.0` with a `MissingInput` when the data is absent):
  - `elo_logit`: `log(p / (1 - p))` of `elo.home_win_chance`.
  - `xg_form`: mean of `xg_for / (xg_for + xg_against)` over the last `FORM_WINDOW` MoneyPuck team games. Missing → `XG_FORM`.
  - `goalie_gsax`: the starter's mean `xg_against - goals_against` over his last `GOALIE_WINDOW` MoneyPuck goalie games. Missing → `GOALIE_FORM`.
  - `rest`: `min(days since the team's previous game start, REST_CAP_DAYS)`; first game in the store → `REST_CAP_DAYS`.

- [ ] **Step 1: Write the failing tests**:
  - `test_elo_matches_sportsbettr`: three games (home 4-1, away 2-3 in OT, home 1-0); assert the exact ratings computed by hand from the formula above, to 1e-9.
  - `test_elo_rejects_non_final_game`.
  - `test_elo_follows_franchise`: an `ARI` game moves the `UTA` rating.
  - `test_elo_new_team_starts_at_initial`: a `VGK` game with no earlier `VGK` game uses `ELO_INITIAL` for `VGK`.
  - `test_features_use_only_games_before_start`: a game seeded after `game.start` does not change any value.
  - `test_xg_form_missing_reports_xg_form`.
  - `test_rest_back_to_back_is_one`, `test_rest_caps_at_four`.
  - A hypothesis test: `elo_expected(a, b) + elo_expected(b, a)` is not 1 (home advantage), but `0 < elo_expected(a, b) < 1` for all finite ratings in [1000, 2000].
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** `elo.py` and `features.py`. One private function per feature.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(model): add nhl elo and game features`.

### Task 12: Logistic fit, `NhlGameModel`, and the `fit` and `game` commands

**Files:**
- Create: `src/hockey_model/game_model/logistic.py`, `src/hockey_model/game_model/nhl_model.py`, `src/hockey_model/game_model/seasons.py`, `src/hockey_model/store/fits.py`
- Modify: `src/hockey_model/cli.py`
- Test: `tests/game_model/test_logistic.py`, `tests/game_model/test_nhl_model.py`, `tests/store/test_fits.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `build_features`, `replay`, `FEATURES` (Task 11); `final_games_before` (Task 10).
- Produces:
  - In `seasons.py` (seasons as the NHL writes them): `ELO_BURN_IN = 20142015`, `FIT_FIRST = 20152016`, `SELECT_TRAIN = (20152016, 20212022)`, `SELECT_VALIDATE = (20222023, 20232024)`, `SCORED = (20242025, 20252026)`. Task 14 imports these.
  - `L2 = 1.0`, `MAX_ITERATIONS = 50`, `TOLERANCE = 1e-9`.
  - `@dataclass(frozen=True) class LogisticFit`: `features: tuple[str, ...]`, `intercept: float`, `elo_weight: float`, `weights: Mapping[str, float]`.
  - `def fit_logistic(rows: Sequence[FeatureRow], wins: Sequence[bool], features: Sequence[str]) -> LogisticFit`: Newton-Raphson (IRLS) on `[1, elo_logit, *features]` with L2 penalty `L2` on every weight except the intercept. Solve the small normal equations with Gaussian elimination in plain Python. Raise `ValueError` when the rows are empty or the lengths differ.
  - `def predict_logistic(fit: LogisticFit, row: FeatureRow) -> float`.
  - `def save_fit(conn, fit: LogisticFit, fit_through_season: int, created_at: datetime) -> int`, `def newest_fit(conn) -> LogisticFit` (raises `StoreError` "run `hockey-model fit` first" when none).
  - `class NhlGameModel` implements `GameModel`: `__init__(self, conn, fit: LogisticFit, goalie_mode: GoalieMode = GoalieMode.READS)`. `predict` replays Elo through `final_games_before(as_of)` (cache the book by the newest game ID), builds features, applies the fit, and returns `GamePrediction`. `credits` holds `MONEYPUCK_CREDIT` when `uses_moneypuck`. A `source_reads` row with `ok=0` newer than the newest `ok=1` row for a source adds `STALE_SOURCE` naming that source.
  - Expected goals: `lambda_home` is the mean of the home team's `xg_for` and the away team's `xg_against` over `FORM_WINDOW` MoneyPuck games; without MoneyPuck rows, use actual goals from `team_games_before`. The interval is the Poisson quantiles at `(1 - level) / 2` and `(1 + level) / 2`.
  - CLI: `hockey-model fit --through-season YYYYYYYY [--features a,b]` (`--features ""` fits the Elo alone) fits on every regular-season and playoff game from `FIT_FIRST` through that season, with Elo replayed from `ELO_BURN_IN`, and saves the fit. `hockey-model game GAME_ID [--level L]` loads the game from the store, uses `newest_fit`, and prints `GamePrediction` as JSON.
- The existing `player`, `team`, `matchup`, and `playoffs` commands keep the `CliRuntime` path unchanged.

- [ ] **Step 1: Write the failing tests**:
  - `test_fit_logistic_recovers_known_weights`: 5,000 rows drawn with seed 0 from a known logistic model with weights (0.1, 1.0, 0.5); the fit is within 0.1 of each weight.
  - `test_fit_logistic_rejects_empty` and `test_fit_logistic_rejects_length_mismatch`.
  - `test_fit_logistic_separable_data_stays_finite`: perfectly separable rows give finite weights (the L2 penalty).
  - `test_nhl_model_sums_to_one`, `test_nhl_model_credits_moneypuck`, `test_nhl_model_reports_stale_source`.
  - `test_nhl_model_makes_no_network_call`: patch `urllib.request.urlopen` to raise; `predict` still returns.
  - `test_newest_fit_missing_raises`.
  - `test_cli_game_prints_prediction` and `test_cli_game_unknown_id` (exit 2, message names the ID and says to run `hockey-model refresh`).
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** the modules and the commands.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(model): add logistic nhl game model and cli`.

### Task 13: Backtest metrics

**Files:**
- Create: `src/hockey_model/backtest/__init__.py`, `src/hockey_model/backtest/metrics.py`
- Test: `tests/backtest/test_metrics.py`

**Interfaces:**
- Produces:
  - `PROB_FLOOR = 1e-6` (probabilities are clamped to `[PROB_FLOOR, 1 - PROB_FLOOR]` before a log).
  - `def log_loss(probs: Sequence[float], wins: Sequence[bool]) -> float`, `def brier(probs, wins) -> float`.
  - `CALIBRATION_BINS = 10`; `@dataclass(frozen=True) class CalibrationBin`: `low: float`, `high: float`, `count: int`, `mean_prob: float`, `win_rate: float`; `def calibration(probs, wins) -> tuple[CalibrationBin, ...]` (empty bins have `count=0` and `nan` means).
  - `BOOTSTRAP_RESAMPLES = 1000`, `BOOTSTRAP_SEED = 0`, `BLOCK_LENGTH`: copy the block length used by sportsbettr `harness/bootstrap_ci.py` `block_bootstrap_log_growth_ci`.
  - `def block_bootstrap_mean_ci(values: Sequence[float], tail: float) -> tuple[float, float]`: the 5th and 95th percentile of the mean of contiguous-block resamples, in game start order. Port the resample loop from sportsbettr `harness/bootstrap_ci.py`.
- [ ] **Step 1: Write the failing tests**: `log_loss([0.5], [True]) == log(2)`, `brier([1.0], [True]) == 0.0`, a probability of 0.0 on a win gives a finite log loss, length mismatch raises `ValueError`, the calibration of 0.05 and 0.95 lands in bins 0 and 9, `block_bootstrap_mean_ci` of a constant list is `(c, c)`, the same input and seed give the same interval, an empty list raises `ValueError`, and a hypothesis test that `low <= high` for any non-empty list of finite floats.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** `metrics.py`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(backtest): add scoring metrics and block bootstrap`.

### Task 14: Backtest runner and the `backtest` command

**Files:**
- Create: `src/hockey_model/backtest/runner.py`
- Modify: `src/hockey_model/cli.py`
- Test: `tests/backtest/test_runner.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 10 to 13.
- Produces:
  - The season constants come from `game_model/seasons.py` (Task 12).
  - `MONEYPUCK_LEAK_NOTE`: one plain sentence that says the MoneyPuck expected-goals model is today's model, and that MoneyPuck possibly trained it on games after the scored seasons.
  - `@dataclass(frozen=True) class ScoredRun`: `name: str`, `games: int`, `log_loss: float`, `brier: float`, `calibration: tuple[CalibrationBin, ...]`, `diff_vs_elo_ci: tuple[float, float] | None`.
  - `@dataclass(frozen=True) class GameRecord`: `game_id: int`, `start: datetime`, `home: str`, `away: str`, `run: str`, `home_win_prob: float`, `home_won: bool`.
  - `@dataclass(frozen=True) class BacktestReport`: `candidate_features: tuple[str, ...]`, `vetoed: tuple[str, ...]`, `tested_features: tuple[str, ...]`, `live_features: tuple[str, ...]`, `validation: Mapping[str, Mapping[str, float]]` (validation log loss by mode, `actual` and `guess`, for `elo` and for Elo plus each feature), `runs: tuple[ScoredRun, ...]`, `success: bool`, `notes: tuple[str, ...]`, `credits: tuple[str, ...]`.
  - `def run_backtest(conn) -> tuple[BacktestReport, list[GameRecord]]`.
  - `def write_game_records(path: Path, records: Sequence[GameRecord]) -> None`: CSV with a header row of the field names; `start` as ISO 8601 UTC.
- Steps inside `run_backtest`:
  1. Replay Elo in start order from `ELO_BURN_IN`. Build each game's features with `as_of = game.start`, before the game updates the Elo. Games of `ELO_BURN_IN` are not used in any fit.
  2. Guess veto: fit Elo alone, and Elo plus each feature alone, on `SELECT_TRAIN`, in `ACTUAL` and in `GUESS` mode; score each on `SELECT_VALIDATE` in the same mode and store all of it in `validation`. Veto a feature whose `GUESS` validation log loss is higher than Elo alone in `GUESS` mode. `ACTUAL` mode drops no feature. `tested_features` is `FEATURES` minus `vetoed`.
  3. For each scored season: fit `tested_features` together on every game from `FIT_FIRST` through the season before it, then score that season's games once.
  4. Score these runs over both scored seasons together: `home_rate` (the home win rate of the fit games), `elo` (sportsbettr Elo, `elo_expected` only, no fit), `model_actual` (`ACTUAL` mode), and `model_guess` (`GUESS` mode, same fits).
  5. `diff_vs_elo_ci` is `block_bootstrap_mean_ci` of the per-game log loss of the run minus that of `elo`.
  6. `success` is true when `model_actual.diff_vs_elo_ci[1] < 0`. This is the one success test. `model_guess` never changes `success`.
  7. `live_features` is `tested_features` when `success`, else `()`, which means the Elo alone ships.
  8. Add `MONEYPUCK_LEAK_NOTE` to `notes` and `MONEYPUCK_CREDIT` to `credits` when a tested feature uses MoneyPuck.
  9. Return one `GameRecord` per scored game for each of the runs `elo`, `model_actual`, and `model_guess`.
- CLI: `hockey-model backtest [--games-out PATH]` prints `BacktestReport` as JSON and exits 0 whether or not `success` is true. With `--games-out`, it also writes the game records. The file has no price data.

- [ ] **Step 1: Write the failing tests** on a small synthetic store (a few teams, seasons shortened by monkeypatching the season constants):
  - `test_backtest_features_see_no_own_result`: spy on `build_features`. For each game, `as_of == game.start`, and the `EloBook` passed in has the same ratings as a replay of the earlier games only.
  - `test_backtest_vetoes_feature_that_hurts_guess_run`: a goalie feature that is right for the actual starter and wrong for the guessed starter is in `vetoed` and not in `tested_features`.
  - `test_backtest_actual_mode_does_not_drop`: a noise feature that does not hurt the guess run stays in `tested_features`.
  - `test_backtest_useful_features_pass_joint_test`: features that shift the true win chance by a known amount give `success` true and `live_features == tested_features`.
  - `test_backtest_failed_joint_test_ships_elo_only`: noise features only give `success` false and `live_features == ()`.
  - `test_backtest_refits_per_season`: the fit for the second scored season includes the first scored season (assert on the `fit_through_season` passed to the fit).
  - `test_backtest_guess_run_does_not_set_success`.
  - `test_backtest_notes_moneypuck_leak` when `xg_form` is tested.
  - `test_cli_backtest_prints_report` and `test_cli_backtest_writes_games_out`: the CSV has one row per scored game per run, and its header names the `GameRecord` fields.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** `runner.py` and the command. Keep each step above in its own function.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(backtest): add walk-forward nhl backtest`.

### Task 15: Real-data backtest run and documentation

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `docs/superpowers/specs/2026-11-02-nhl-game-model-design.md`
- Create: `docs/backtest/2026-11-nhl-game-model.md`

- [ ] **Step 1: Backfill.** Run `HOCKEY_MODEL_DB=<path> uv run hockey-model refresh --backfill-from 2014-10-01`. The NHL boxscores are about 16,000 requests, so run it in the background and watch its output. Run it again until it exits 0.
- [ ] **Step 2: Run** `uv run hockey-model backtest --games-out .tmp/backtest-games.csv > .tmp/backtest.json`. Record the vetoed, tested, and live features, every run's log loss, Brier score, and interval, the `success` value, and the MoneyPuck note in `docs/backtest/2026-11-nhl-game-model.md`. A failed success test is a result to record, not a reason to change the scored seasons.
- [ ] **Step 3: Fit** the live model: `uv run hockey-model fit --through-season 20252026 --features <live_features>`. An empty `live_features` fits the Elo alone.
- [ ] **Step 4: Update `README.md`**: the README no longer says that hockey-model ships no production model. Add the `refresh`, `fit`, `game`, and `backtest` commands, the `HOCKEY_MODEL_DB` setting, the data sources, and the MoneyPuck credit and its non-commercial term.
- [ ] **Step 5: Update the spec** for any change that the work made to a decision in it.
- [ ] **Step 6: Add** a `CHANGELOG.md` entry under `Unreleased` with the `keepachangelog` skill.
- [ ] **Step 7: Commit** `docs: document nhl game model and backtest result`.
