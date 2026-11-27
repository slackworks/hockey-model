-- Source tables are append-only: each read carries fetched_at as ISO UTC, and a later
-- read adds a row. The exceptions are skater_games and goalie_games, which are written
-- from a final boxscore and keyed on the game. A row stays as first written, and a later
-- boxscore adds only the rows that are missing.

CREATE TABLE schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE source_reads (
    source TEXT,
    url TEXT,
    fetched_at TEXT NOT NULL,
    ok INTEGER,
    error TEXT,
    permanent INTEGER
);

CREATE TABLE games (
    game_id INTEGER,
    season INTEGER,
    game_type INTEGER,
    start_utc TEXT,
    home TEXT,
    away TEXT,
    state TEXT,
    home_score INTEGER,
    away_score INTEGER,
    last_period TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, fetched_at)
);

CREATE TABLE players (
    nhl_id INTEGER,
    full_name TEXT,
    position TEXT,
    team TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (nhl_id, fetched_at)
);

-- Written from a final boxscore, so keyed on the game only. An existing row is kept.
CREATE TABLE skater_games (
    game_id INTEGER,
    team TEXT,
    nhl_id INTEGER,
    toi_seconds INTEGER,
    goals INTEGER,
    assists INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, nhl_id)
);

CREATE TABLE goalie_games (
    game_id INTEGER,
    team TEXT,
    nhl_id INTEGER,
    toi_seconds INTEGER,
    started INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, nhl_id)
);

CREATE TABLE mp_team_games (
    game_id INTEGER,
    team TEXT,
    game_date TEXT,
    xg_for REAL,
    xg_against REAL,
    shot_attempts_for INTEGER,
    shot_attempts_against INTEGER,
    -- 0 only for a row that a version 1 store wrote, before the counts existed. The upgrade
    -- sets it. Every other row must carry both counts. Readers do not check this flag:
    -- they filter on IS NOT NULL, which is the same test for a row the upgrade left unknown.
    attempts_known INTEGER NOT NULL DEFAULT 1 CHECK (attempts_known IN (0, 1)),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, team, fetched_at),
    CHECK (
        attempts_known = 0
        OR (shot_attempts_for IS NOT NULL AND shot_attempts_against IS NOT NULL)
    )
);

CREATE TABLE mp_goalie_games (
    game_id INTEGER,
    nhl_id INTEGER,
    team TEXT,
    xg_against REAL,
    goals_against INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, nhl_id, fetched_at)
);

CREATE TABLE starter_reads (
    game_id INTEGER,
    team TEXT,
    source TEXT,
    goalie_name TEXT,
    status TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE injury_reads (
    team TEXT,
    player_name TEXT,
    status TEXT,
    return_date TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE model_fits (
    fit_id INTEGER PRIMARY KEY,
    fit_through_season INTEGER,
    features TEXT,
    weights TEXT,
    created_at TEXT NOT NULL
);

-- Which fit a name serves. Append-only, so a label can move to a newer fit and its earlier
-- assignment stays readable. A row is a statement by the person who saved the fit, not a
-- derived value, so a later research fit that carries no label cannot replace the live one.
CREATE TABLE fit_labels (
    label TEXT NOT NULL,
    fit_id INTEGER NOT NULL REFERENCES model_fits (fit_id),
    set_at TEXT NOT NULL
);

-- The body of each distinct fetch, once per hash. ``body`` holds the bytes that arrived,
-- compressed with zlib at level 6, and ``size`` is the uncompressed byte count.
CREATE TABLE payloads (
    sha256 TEXT PRIMARY KEY,
    body BLOB NOT NULL,
    size INTEGER NOT NULL
);

-- One row for every fetch, also when the bytes did not change. A row that failed keeps the
-- bytes that did arrive, and its stage: http_error, shape_error, or store_error.
CREATE TABLE observations (
    obs_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    key TEXT,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'http_error', 'shape_error', 'store_error')),
    sha256 TEXT REFERENCES payloads (sha256),
    parser TEXT NOT NULL,
    error TEXT
);

-- One row for each distinct state of a final boxscore that a source gave (#82). ``derivation``
-- names one parse of the archived payloads: 1 is the first, and a ``store rebuild`` adds the
-- next. ``revision`` counts the states inside one derivation, and a revision comes from the
-- observation ``obs_id``. A revision that an upgrade wrote has no bytes: its parser is
-- ``legacy:unavailable``. ``skater_games`` and ``goalie_games`` keep the first final rows.
CREATE TABLE boxscore_revisions (
    game_id INTEGER NOT NULL,
    derivation INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    obs_id INTEGER NOT NULL REFERENCES observations (obs_id),
    parser TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    completeness TEXT NOT NULL CHECK (completeness IN ('complete', 'incomplete')),
    identity TEXT NOT NULL CHECK (identity IN ('matched', 'mismatched')),
    PRIMARY KEY (game_id, derivation, revision)
);

CREATE TABLE skater_game_revisions (
    game_id INTEGER NOT NULL,
    derivation INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    team TEXT NOT NULL,
    nhl_id INTEGER NOT NULL,
    toi_seconds INTEGER,
    goals INTEGER,
    assists INTEGER,
    PRIMARY KEY (game_id, derivation, revision, nhl_id),
    FOREIGN KEY (game_id, derivation, revision)
        REFERENCES boxscore_revisions (game_id, derivation, revision)
);

CREATE TABLE goalie_game_revisions (
    game_id INTEGER NOT NULL,
    derivation INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    team TEXT NOT NULL,
    nhl_id INTEGER NOT NULL,
    toi_seconds INTEGER,
    started INTEGER,
    PRIMARY KEY (game_id, derivation, revision, nhl_id),
    FOREIGN KEY (game_id, derivation, revision)
        REFERENCES boxscore_revisions (game_id, derivation, revision)
);

-- Indexes for the as-of reads, which filter by time, team, and player.
CREATE INDEX IF NOT EXISTS games_start ON games (start_utc);
CREATE INDEX IF NOT EXISTS goalie_games_team ON goalie_games (team, started);
CREATE INDEX IF NOT EXISTS mp_team_games_team ON mp_team_games (team);
CREATE INDEX IF NOT EXISTS mp_goalie_games_player ON mp_goalie_games (nhl_id);
CREATE INDEX IF NOT EXISTS starter_reads_game ON starter_reads (game_id, team);
CREATE INDEX IF NOT EXISTS injury_reads_time ON injury_reads (fetched_at);
CREATE INDEX IF NOT EXISTS observations_key ON observations (source, key, fetched_at);
