# Payload archive and boxscore revisions (#82)

## Goal

Keep the bytes that each source returned, so a parser fix or a new feature can use the data as it was seen, and so an old prediction can name its inputs.
Keep each revision of a final boxscore, instead of only the first one.

This design also gives the archive that the play-by-play dataset uses ([pbp-shift-dataset.md](pbp-shift-dataset.md), #35).
Build this one first.

## Non-goals

- No change to what the live model reads. `skater_games` and `goalie_games` keep their first-final rows.
- No recovery of old bytes. A read made before this change has no payload, and the archive says so.
- No price data. The ESPN sanitizer below runs before the hash.

## Source inventory

| Source name (`source_reads.source`) | Payload | Archive rule |
| --- | --- | --- |
| NHL score day | JSON | full payload |
| NHL boxscore | JSON | full payload, plus the revisit schedule below |
| ESPN scoreboard, starters, injuries | JSON | sanitized payload |
| Daily Faceoff starting goalies | HTML | full payload |
| MoneyPuck `all_teams.csv` | CSV, large, changes daily | row level only: the tables of [moneypuck-situations.md](moneypuck-situations.md) keep each changed row. The full file is not kept. |
| MoneyPuck goalie ZIP | ZIP | full payload, because one season file is small and changes only while the season runs |

The builder MUST read `store/refresh_nhl.py`, `store/refresh_other.py`, and `sources/*.py` and list the exact source names in a constant `ARCHIVED_SOURCES` in the new module.
A source that is not in the list MUST be named in a test as "not archived", with the reason, so a new source cannot skip the archive by accident.

## Storage

Schema version 4 (after version 3 of the MoneyPuck design), through `hockey-model store upgrade`.

```sql
CREATE TABLE payloads (
    sha256 TEXT PRIMARY KEY,          -- hash of the sanitized, uncompressed bytes
    body BLOB NOT NULL,               -- zlib.compress(sanitized bytes, level 6)
    size INTEGER NOT NULL             -- uncompressed byte count
);
CREATE TABLE observations (
    obs_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    key TEXT,                         -- the game id or day, when the source has one
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'http_error', 'shape_error', 'parse_error')),
    sha256 TEXT REFERENCES payloads (sha256),   -- NULL when no body arrived
    parser TEXT NOT NULL,             -- parser name and version, such as 'nhl_boxscore:1'
    error TEXT
);
CREATE INDEX observations_key ON observations (source, key, fetched_at);
CREATE TABLE boxscore_revisions (
    game_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,        -- 1 for the first final boxscore
    obs_id INTEGER NOT NULL REFERENCES observations (obs_id),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, revision)
);
CREATE TABLE skater_game_revisions (    -- same columns as skater_games, plus revision
    game_id INTEGER, revision INTEGER, team TEXT, nhl_id INTEGER,
    toi_seconds INTEGER, goals INTEGER, assists INTEGER,
    PRIMARY KEY (game_id, revision, nhl_id)
);
CREATE TABLE goalie_game_revisions (    -- same columns as goalie_games, plus revision
    game_id INTEGER, revision INTEGER, team TEXT, nhl_id INTEGER,
    toi_seconds INTEGER, started INTEGER,
    PRIMARY KEY (game_id, revision, nhl_id)
);
```

Rules:

1. Every fetch writes one `observations` row, also when the bytes did not change. A, then B, then A gives three rows and two `payloads` rows.
2. A `payloads` row is written only when its hash is new (`INSERT OR IGNORE`).
3. The upgrade writes revision 1 for each stored final boxscore from `skater_games` and `goalie_games`, with an `observations` row of status `ok`, `sha256` NULL, and `parser` `'legacy:unavailable'`. It never invents bytes or a fetch time: `fetched_at` is the `fetched_at` of the existing rows.
4. A new boxscore revision is written only when its parsed rows differ from the newest revision of that game. An unchanged revisit adds an observation and no revision.

## Code

1. New module `store/archive.py`:
   - `record_observation(conn, *, source, url, key, fetched_at, status, body, parser, error) -> int` returns `obs_id`.
   - `payload_bytes(conn, sha256) -> bytes` returns the uncompressed bytes. A missing hash raises `StoreError` that names the hash.
   - `observations_for(conn, source, key, *, before: datetime | None) -> list[Observation]`.
2. New module `sources/sanitize.py` with `strip_market_fields(payload: object) -> object`. It removes every key in `MARKET_KEYS = ("pickcenter", "odds", "againstTheSpread")` at any depth, and returns a new object. The ESPN parsers call it, and the archive hashes its `json.dumps(..., sort_keys=True, separators=(",", ":"))` bytes.
3. `HttpClient` stays as it is. `RefreshRun.attempt` passes a recording wrapper to the unit: the wrapper calls the client and keeps `(source, url, bytes, fetched_at)` for each request of the unit. After the unit ends, `attempt` writes the observations in the same transaction as the parsed rows. A failed unit writes its observations in the `except` branch, after the rollback, with the failure status and the bytes that did arrive.
4. Each parser module gets a `PARSER = "<name>:<version>"` constant. Change the version when a parser change can change a parsed row.
5. Boxscore revisit: `REVISIT_AFTER_DAYS = (2, 14)` in `store/refresh_nhl.py`. A final game is read again when the newest observation is older than the next revisit day and the game started at least that many days ago. A revisit uses the same unit, failure, and abandon rules as the first read. The `boxscores_read` set of `RefreshRun` still limits one game to one request in one refresh.
6. New reader `boxscore_rows(conn, game_id, *, policy)` in `store/reads.py`. `policy` is `FIRST`, `LATEST`, or `AsOf(instant)`. It never mixes rows of two revisions. No current caller changes: they keep the first-final tables.
7. New command `hockey-model store rebuild --source S --parser P`: parse the stored payloads of one source again with the current parser, and write the derived rows as a new derivation. It makes no network request. The first user is #35.

## Tests

- Same bytes twice: two observations, one payload.
- A, B, A: three observations, two payloads, and `observations_for` returns them in fetch order.
- A changed boxscore adds revision 2. `boxscore_rows(..., policy=AsOf(t))` before the revisit returns revision 1.
- A malformed response stores status `shape_error` and keeps the bytes. An HTTP failure stores `http_error` and no bytes.
- An ESPN fixture that holds `pickcenter`, `odds`, and `againstTheSpread` at two depths: the stored body has none of them (decompress and search the bytes).
- The upgrade from version 3 keeps every row, writes revision 1 for every final boxscore, and a second upgrade run is refused with the existing message.
- The backtest report and CSV on the seeded store are byte-identical before and after.

## Size

Measure the compressed size of one day of payloads on the real store before the backfill, and write it in the pull request.
The play-by-play design estimates about 600 MB for its backfill. The payloads of this design add the daily NHL, ESPN, and Daily Faceoff reads.

## Calls made

- MoneyPuck `all_teams.csv` is archived at the row level only. A full copy each day costs gigabytes in a year for a file that MoneyPuck can serve again. The columns that the wide tables do not keep are an unsupported capture case, and the README says so.
- Versioned boxscores live beside the first-final tables, so the live model cannot change by accident. A later issue can move a reader to `LATEST` on purpose.
- The revisit days are the same as the play-by-play revisit days in #35, so one schedule serves both.
