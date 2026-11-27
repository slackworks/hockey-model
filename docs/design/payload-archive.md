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

The builder MUST read `store/refresh_nhl.py`, `store/refresh_other.py`, and `sources/*.py` and list the exact source names in the `KNOWN_SOURCES` constant of `store/source_reads.py`, which already holds every source that a refresh records.
`RefreshRun.attempt` MUST raise `StoreError` for a source that the list does not hold, before it makes any request.
A source that is not in the list MUST be named in a test as "not recorded", with the reason, so a new source cannot skip the archive by accident.

## Storage

Schema version 9 holds `payloads` and `observations`. Schema version 10 holds the three revision tables. Both come through `hockey-model store upgrade`.

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
    status TEXT NOT NULL CHECK (status IN ('ok', 'http_error', 'shape_error', 'store_error')),
    sha256 TEXT REFERENCES payloads (sha256),   -- NULL when no body arrived
    parser TEXT NOT NULL,             -- parser name and version, such as 'nhl_boxscore:1'
    error TEXT
);
CREATE INDEX observations_key ON observations (source, key, fetched_at);
CREATE TABLE boxscore_revisions (
    game_id INTEGER NOT NULL,
    derivation INTEGER NOT NULL,      -- 1 for the first parse of the payloads; a rebuild adds the next
    revision INTEGER NOT NULL,        -- 1 for the first final boxscore of the derivation
    obs_id INTEGER NOT NULL REFERENCES observations (obs_id),
    parser TEXT NOT NULL,             -- 'nhl_boxscore:2', or 'legacy:unavailable' for a row with no bytes
    fetched_at TEXT NOT NULL,         -- the time of the observation, never a time of the rebuild
    completeness TEXT NOT NULL CHECK (completeness IN ('complete', 'incomplete')),
    identity TEXT NOT NULL CHECK (identity IN ('matched', 'mismatched', 'unverified')),
    PRIMARY KEY (game_id, derivation, revision)
);
CREATE TABLE skater_game_revisions (    -- the columns of skater_games, plus derivation and revision
    game_id INTEGER, derivation INTEGER, revision INTEGER, team TEXT, nhl_id INTEGER,
    toi_seconds INTEGER, goals INTEGER, assists INTEGER,
    PRIMARY KEY (game_id, derivation, revision, nhl_id)
);
CREATE TABLE goalie_game_revisions (    -- the columns of goalie_games, plus derivation and revision
    game_id INTEGER, derivation INTEGER, revision INTEGER, team TEXT, nhl_id INTEGER,
    toi_seconds INTEGER, started INTEGER,
    PRIMARY KEY (game_id, derivation, revision, nhl_id)
);
```

Rules:

1. Every fetch writes one `observations` row, also when the bytes did not change. A, then B, then A gives three rows and two `payloads` rows. A fetch that a `Reading.expected_absence` excuses still writes its row, with the status of the failure, because the request ran.
2. A `payloads` row is written only when its hash is new (`INSERT OR IGNORE`).
3. The 9->10 upgrade writes revision 1 of derivation 1 for each stored final boxscore from `skater_games` and `goalie_games`, with an `observations` row of status `ok`, `sha256` NULL, and `parser` `'legacy:unavailable'`. It never invents bytes or a fetch time: `fetched_at` is the newest `fetched_at` of the rows of the game, because that is when the whole boxscore was in the store. `completeness` is `complete` when both teams have a skater line and a goalie line. A game whose rows share one fetch time, and that has an `ok` observation with bytes at that time, points at that observation and its parser, and no legacy observation is written. Rows of two reads can sit under one legacy revision, because `write_boxscore` adds a missing team from a later read, so that revision is the one place where two reads mix. `identity` is `matched` when every team of the rows is the home or the away team of the stored game, `mismatched` when one team is neither, and `unverified` when the store holds no game row to compare. A revision that a source gives later is `matched` always, because a boxscore that fails the identity check writes no revision.
4. A new boxscore revision is written only when its parsed rows differ from the newest revision of that game. An unchanged revisit adds an observation and no revision. A boxscore that fails the parser or the identity check writes no revision, and its observation keeps the bytes with the status `shape_error`.
5. A rebuild writes a new derivation for a game only when the revisions that the archived bytes give now differ from the newest derivation. A revision with no bytes carries over, so the first revision of a game that began before the archive stays revision 1. A second rebuild with the same parser changes nothing. A payload that the first read accepted and the parser now refuses stops the rebuild with a `StoreError` that names the game and the observation, so a parser regression never replaces a good derivation. A payload that was a `shape_error` and stays one is named in `refused` of the report. An earlier derivation stays, so a forecast can name the rows it read. `boxscore_rows` reads the newest derivation unless a caller pins one. `AsOf(instant)` selects by fetch time and reads the newest derivation, so it gives the current parse of what the store held at the instant. A derivation records no write time. A forecast that must be replayed exactly records `(derivation, revision)` and pins the derivation. A rebuild with `--allow-refused` drops a payload that an earlier parser accepted and the new parser refuses, and the report names it.

## Code

1. New module `store/archive.py`:
   - `record_observation(conn, fetch: Fetch) -> int` writes one row and returns `obs_id`. `Fetch` holds the source, URL, key, fetch time, status, parser, body, and error of one request.
   - `payload_bytes(conn, sha256) -> bytes` returns the uncompressed bytes. A missing hash raises `StoreError` that names the hash.
   - `observations_for(conn, source, key, *, before: datetime | None) -> list[Observation]`.
2. New module `sources/sanitize.py` with `strip_market_fields(payload: object) -> object`. It removes every key in `MARKET_KEYS = ("pickcenter", "odds", "againstTheSpread")` at any depth, and returns a new object. The capture wrapper calls it for a source in `MARKET_SOURCES`, so the parser and the archive both read the sanitized body, and the archive hashes its `canonical_bytes` (`json.dumps(..., sort_keys=True, separators=(",", ":"))`). The source name decides that, not the caller, so a new source cannot keep its odds by accident.
3. `HttpClient` stays as it is. `RefreshRun.attempt` passes a `RecordingClient` wrapper to the unit: the wrapper calls the client and keeps the URL, status, body, and error of each request of the unit. After the unit ends, `attempt` writes the observations in the same transaction as the parsed rows. A failed unit writes its observations in the `except` branch, after the rollback. A derive hook that cannot write raises `DeriveError`: the unit rolls back, the observation and its bytes are written after the rollback as a `store_error` with the error text, so a revisit that failed here is still due, no failed read is recorded, and the refresh stops until the code is fixed. The failure is local, so it gets no backoff. A request that failed keeps its own status, and a parser failure marks the bytes that the parser rejected.
4. Each parser module gets a `PARSER = "<name>:<version>"` constant. Change the version when a parser change can change a parsed row.
5. Boxscore revisit: `REVISIT_AFTER_DAYS = (2, 14)` in `store/refresh_nhl.py`. A final game is read again when a revisit day has passed and the newest good observation is older than that day. A refresh reads at most `MAX_REVISITS` games, the oldest due first, and notes the rest. The step runs in the refresh without a backfill. A revisit that fails twice, at least half a day apart, makes the game a known gap like a first read, so the revisit step leaves it out, and `--retry-skipped` reads it again. A revisit uses the same unit, failure, and abandon rules as the first read. The `boxscores_read` set of `RefreshRun` still limits one game to one request in one refresh.
6. New reader `boxscore_rows(conn, game_id, *, policy)` in `store/reads.py`. `policy` is `FIRST`, `LATEST`, or `AsOf(instant)`, and `AsOf` takes the newest revision fetched at or before the instant. It never mixes rows of two revisions. The function lives in `store/boxscore_revisions.py`. No current caller changes: they keep the first-final tables.
7. New command `hockey-model store rebuild --source S --parser P`: parse the stored payloads of one source again with the current parser, and write the derived rows as a new derivation. It makes no network request. The first user is #35.

## Tests

- Same bytes twice: two observations, one payload.
- A, B, A: three observations, two payloads, and `observations_for` returns them in fetch order.
- A changed boxscore adds revision 2. `boxscore_rows(..., policy=AsOf(t))` before the revisit returns revision 1.
- A malformed response stores status `shape_error` and keeps the bytes. An HTTP failure stores `http_error` and no bytes.
- An ESPN fixture that holds `pickcenter`, `odds`, and `againstTheSpread` at two depths: the stored body has none of them (decompress and search the bytes).
- The upgrade from the previous version keeps every row, writes revision 1 for every final boxscore, and a second upgrade run is refused with the existing message.
- The backtest report and CSV on the seeded store are byte-identical before and after.

## Size

Measure the compressed size of one day of payloads on the real store before the backfill, and write it in the pull request.
The play-by-play design estimates about 600 MB for its backfill. The payloads of this design add the daily NHL, ESPN, and Daily Faceoff reads.

## Calls made

- MoneyPuck `all_teams.csv` is archived at the row level only. A full copy each day costs gigabytes in a year for a file that MoneyPuck can serve again. The columns that the wide tables do not keep are an unsupported capture case, and the README says so.
- Versioned boxscores live beside the first-final tables, so the live model cannot change by accident. A later issue can move a reader to `LATEST` on purpose.
- The revisit days are the same as the play-by-play revisit days in #35, so one schedule serves both.

## Slices

The first slice ships the storage, the sanitizer, and the capture (Code steps 1 to 4).
It is schema version 9.
`store/archive.py` holds `record_observation`, `payload_bytes`, `observations_for`, and the
market-source list.
`store/recording.py` holds the capture wrapper and the `Reading` value that names a parser.
The MoneyPuck team file writes its observation and no bytes, so the one source with no payload
is named in the code and held by a test.

The second slice (#326, schema version 10) holds the versioned boxscores (Code steps 5 and 6), the `store rebuild`
command (step 7), and the revisit policy. `store/boxscore_revisions.py` holds the writer and the reader,
`store/boxscore_revisit.py` chooses the games, and `store/boxscore_rebuild.py` holds the rebuild.
