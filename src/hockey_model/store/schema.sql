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

-- Indexes for the as-of reads, which filter by time, team, and player.
CREATE INDEX IF NOT EXISTS games_start ON games (start_utc);
CREATE INDEX IF NOT EXISTS goalie_games_team ON goalie_games (team, started);
CREATE INDEX IF NOT EXISTS mp_team_games_team ON mp_team_games (team);
CREATE INDEX IF NOT EXISTS mp_goalie_games_player ON mp_goalie_games (nhl_id);
CREATE INDEX IF NOT EXISTS starter_reads_game ON starter_reads (game_id, team);
CREATE INDEX IF NOT EXISTS injury_reads_time ON injury_reads (fetched_at);
