# MoneyPuck strength splits and extra columns

Shared input for #40, #41, #42, #45, #46, #47, #48, #57, and #58.
Build it once, in its own pull request, before the first of those issues.

## Problem

The store downloads two MoneyPuck files: `all_teams.csv` (`TEAM_GAMES_URL`) and one goalie ZIP for each season (`goalie_games_url`).
Each file has one row for each team game or goalie game and each `situation`: `all`, `5on5`, `5on4`, `4on5`, and `other`.
The parser in `sources/moneypuck.py` keeps only the `all` rows (`ALL_SITUATIONS`) and a few columns.
The team file has 111 columns. It holds ice time, shots on goal, rebounds, freezes, penalties, faceoffs, and a score-venue adjusted xG for each situation.
The goalie file holds ice time, unblocked shots, shots on goal, rebounds, and freezes, with an expected value for most of them.

These columns let several research issues start before the play-by-play dataset (#35) exists.
They need no new request and no new source approval, because the files are the same downloads.

## Scope

- Parse and store the columns below for every situation.
- Keep `mp_team_games` and `mp_goalie_games` as they are. The live model reads them, and its results MUST NOT change.
- No feature reads the new tables in this pull request.

## Columns

Team file, for each of `For` and `Against` (the stored name is snake case, such as `xg_for`):

`iceTime` (one column, no side), `xGoals`, `scoreVenueAdjustedxGoals`, `flurryAdjustedxGoals`, `shotsOnGoal`, `missedShots`, `blockedShotAttempts`, `shotAttempts`, `unblockedShotAttempts`, `goals`, `rebounds`, `reboundGoals`, `xRebounds`, `freeze`, `xFreeze`, `playStopped`, `xPlayStopped`, `penalties`, `penalityMinutes` (MoneyPuck spells it so), `faceOffsWon`, `lowDangerShots`, `mediumDangerShots`, `highDangerShots`.

Goalie file:

`icetime`, `xGoals`, `goals`, `unblocked_shot_attempts`, `xOnGoal`, `ongoal`, `xRebounds`, `rebounds`, `xFreeze`, `freeze`, `xPlayStopped`, `playStopped`, `lowDangerShots`, `mediumDangerShots`, `highDangerShots`, `lowDangerxGoals`, `mediumDangerxGoals`, `highDangerxGoals`, `lowDangerGoals`, `mediumDangerGoals`, `highDangerGoals`.

Keep the two lists as tuples of `(csv_name, column_name, kind)` in the new parser module, so the parser, the DDL, and the tests read one list.

## Storage

Schema version 3, added through `hockey-model store upgrade` (`store/upgrade.py`), with the backup and the one-transaction rule that the upgrade already has.

```sql
CREATE TABLE mp_team_situations (
    game_id INTEGER NOT NULL,
    team TEXT NOT NULL,
    situation TEXT NOT NULL CHECK (situation IN ('all', '5on5', '5on4', '4on5', 'other')),
    -- one REAL or INTEGER column for each entry of the team list
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, team, situation, fetched_at)
);
CREATE TABLE mp_goalie_situations (
    game_id INTEGER NOT NULL,
    nhl_id INTEGER NOT NULL,
    team TEXT NOT NULL,
    situation TEXT NOT NULL CHECK (situation IN ('all', '5on5', '5on4', '4on5', 'other')),
    -- one column for each entry of the goalie list
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, nhl_id, situation, fetched_at)
);
```

Add `latest_mp_team_situations` and `latest_mp_goalie_situations` to `views.sql`, in the same form as `latest_mp_team_games`.
Write a row with the same rule that `store/moneypuck_writes.py` uses for the existing tables: read that module first and copy its rule for an unchanged row.
An empty cell is NULL, never zero.

## Code

1. Move the CSV helpers that both parsers need (`_number`, `_count`, `_check_columns`, `_game_id`, `_game_date`, `_csv_text`) from `sources/moneypuck.py` into a new public module `sources/moneypuck_csv.py`. The module budget forbids a cross-module import of a private name.
2. New module `sources/moneypuck_situations.py` with `parse_team_situations(text: str)` and `parse_goalie_situations(data: bytes)`. Each returns frozen dataclass rows. A missing column raises `SourceShapeError` that names the column. A bad number raises `SourceParseError` that names the line and the column.
3. `refresh` parses the same downloaded bytes twice: once for the existing tables, once for the new tables. It makes no extra request.
4. `refresh --reread-moneypuck` fills the new tables for the stored seasons.
5. `store/reads.py` gets no new reader in this pull request. Each research issue adds the reader it needs.

## Tests

- The fixtures `tests/fixtures/moneypuck/all_teams_sample.csv` and `goalies_2025_sample.zip` already hold all five situations. Parse them and assert the values of one hand-checked row for each situation.
- A renamed column raises `SourceShapeError` with the full message (owner rule in #31).
- An empty cell is stored as NULL.
- `store upgrade` from version 2 keeps every row and every fit ID, and adds the empty tables.
- The backtest report and CSV on the seeded store are byte-identical before and after.

## Limits that each user of this data MUST state

- Every xG column comes from today's MoneyPuck model. `MONEYPUCK_LEAK_NOTE` applies to each feature that reads one.
- The goalie files have no shots against by shot type or location. Event-level work still needs #35.
- MoneyPuck defines its own rebound, freeze, and danger zones. A feature that uses them uses MoneyPuck's definitions, and the report says so.

## Calls made

- Explicit columns, not one JSON column, so that SQL can filter and sum them and a renamed MoneyPuck column fails loudly.
- A schema version bump through the existing upgrade path, not DDL on connect, because a consumer open must not run DDL (#80).
