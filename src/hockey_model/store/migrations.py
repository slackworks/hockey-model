"""The ordered schema changes of a store, one entry for each version step.

``store/upgrade.py`` applies them in order inside one transaction. Each entry is the change
from one version to the next, and each statement runs once.
"""

import re

from hockey_model.store.db import StoreError


def mp_team_games_ddl(table: str) -> str:
    """Return the ``mp_team_games`` DDL of version 7 under another table name.

    The 1->2 and the 3->4 steps both rebuild this table, with the four counts. One definition
    keeps the two results identical, and the test compares each result against a fresh store.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise StoreError(
            f"rebuild mp_team_games: {table!r} is not a table name; a name holds letters, "
            "digits, and underscores only. Pass a literal from this module."
        )
    return f"""
CREATE TABLE {table} (
    game_id INTEGER,
    team TEXT,
    game_date TEXT,
    xg_for REAL,
    xg_against REAL,
    shot_attempts_for INTEGER,
    shot_attempts_against INTEGER,
    unblocked_for INTEGER,
    unblocked_against INTEGER,
    attempts_known INTEGER NOT NULL DEFAULT 1 CHECK (attempts_known IN (0, 1)),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, team, fetched_at),
    CHECK (
        attempts_known = 0
        OR (shot_attempts_for IS NOT NULL AND shot_attempts_against IS NOT NULL
            AND unblocked_for IS NOT NULL AND unblocked_against IS NOT NULL)
    )
)
"""


_MP_TEAM_GAMES_V2 = mp_team_games_ddl("mp_team_games_v2")
_MP_TEAM_GAMES_V4 = mp_team_games_ddl("mp_team_games_v4")
# The 4->5 step rebuilds these two tables for the same reason the 3->4 step rebuilds
# mp_team_games: schema.sql changed without a migration step, so an upgraded store kept the
# weaker shape. Both tables hold no key that a rebuild must preserve beyond the fit ids.
_SOURCE_READS_V5 = """
CREATE TABLE source_reads_v5 (
    source TEXT,
    url TEXT,
    fetched_at TEXT NOT NULL,
    ok INTEGER,
    error TEXT
)
"""
_MODEL_FITS_V5 = """
CREATE TABLE model_fits_v5 (
    fit_id INTEGER PRIMARY KEY,
    fit_through_season INTEGER,
    features TEXT,
    weights TEXT,
    created_at TEXT NOT NULL
)
"""
# The 7->8 step rebuilds mp_team_games without the two unblocked counts, because a CHECK that
# names a column blocks DROP COLUMN. A row keeps its counts and its attempts_known flag.
_MP_TEAM_GAMES_V8 = """
CREATE TABLE mp_team_games_v8 (
    game_id INTEGER,
    team TEXT,
    game_date TEXT,
    xg_for REAL,
    xg_against REAL,
    shot_attempts_for INTEGER,
    shot_attempts_against INTEGER,
    attempts_known INTEGER NOT NULL DEFAULT 1 CHECK (attempts_known IN (0, 1)),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, team, fetched_at),
    CHECK (
        attempts_known = 0
        OR (shot_attempts_for IS NOT NULL AND shot_attempts_against IS NOT NULL)
    )
)
"""
# The archive of the unblocked counts that the 7->8 step found, keyed by the primary key of the
# row they came from. A fresh store has none, so schema.sql does not create the table.
_RETIRED_MP_UNBLOCKED = """
CREATE TABLE IF NOT EXISTS retired_mp_unblocked (
    game_id INTEGER,
    team TEXT,
    fetched_at TEXT NOT NULL,
    unblocked_for INTEGER,
    unblocked_against INTEGER,
    PRIMARY KEY (game_id, team, fetched_at)
)
"""
# The archive of the values that the 6->7 step found in the dropped columns. A fresh store
# has no such value, so schema.sql does not create the table.
_RETIRED_NHL_IDS = """
CREATE TABLE IF NOT EXISTS retired_nhl_ids (
    source_table TEXT NOT NULL,
    observation TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    nhl_id INTEGER NOT NULL
)
"""
# A stored row is known when it carries all four counts. The flag is derived, so the
# rebuild writes it from the counts instead of trusting a column that a version 2 store
# may not have.
V4_COUNT = (
    "CASE WHEN shot_attempts_for IS NOT NULL AND shot_attempts_against IS NOT NULL"
    " AND unblocked_for IS NOT NULL AND unblocked_against IS NOT NULL"
    " THEN 1 ELSE 0 END"
)

# The change from each version to the next, in order. Each change is one statement. The
# views are dropped before the first change and rebuilt after the commit.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        _MP_TEAM_GAMES_V2,
        "INSERT INTO mp_team_games_v2 (game_id, team, game_date, xg_for, xg_against,"
        " fetched_at, attempts_known)"
        " SELECT game_id, team, game_date, xg_for, xg_against, fetched_at, 0"
        " FROM mp_team_games",
        "DROP TABLE mp_team_games",
        "ALTER TABLE mp_team_games_v2 RENAME TO mp_team_games",
        "CREATE INDEX IF NOT EXISTS mp_team_games_team ON mp_team_games (team)",
    ),
    2: (
        "CREATE TABLE IF NOT EXISTS fit_labels ("
        " label TEXT NOT NULL,"
        " fit_id INTEGER NOT NULL REFERENCES model_fits (fit_id),"
        " set_at TEXT NOT NULL)",
    ),
    # A version 2 store that the earlier code wrote has no attempts_known column, so a
    # rebuild is the one change that works for both version 2 shapes. It derives the flag
    # from the counts, which is the same test the readers use.
    #
    # The five indexes below reached schema.sql after version 1 and no step created them, so
    # an upgraded store read every table that lacks one as a full scan. A test compares the
    # index names of an upgraded store with a fresh one.
    3: (
        _MP_TEAM_GAMES_V4,
        "INSERT INTO mp_team_games_v4 (game_id, team, game_date, xg_for, xg_against,"
        " shot_attempts_for, shot_attempts_against, unblocked_for, unblocked_against,"
        " attempts_known, fetched_at) SELECT game_id, team, game_date, xg_for, xg_against,"
        " shot_attempts_for, shot_attempts_against, unblocked_for, unblocked_against,"
        f" {V4_COUNT}, fetched_at FROM mp_team_games",
        "DROP TABLE mp_team_games",
        "ALTER TABLE mp_team_games_v4 RENAME TO mp_team_games",
        "CREATE INDEX IF NOT EXISTS mp_team_games_team ON mp_team_games (team)",
        "CREATE INDEX IF NOT EXISTS games_start ON games (start_utc)",
        "CREATE INDEX IF NOT EXISTS goalie_games_team ON goalie_games (team, started)",
        "CREATE INDEX IF NOT EXISTS mp_goalie_games_player ON mp_goalie_games (nhl_id)",
        "CREATE INDEX IF NOT EXISTS starter_reads_game ON starter_reads (game_id, team)",
        "CREATE INDEX IF NOT EXISTS injury_reads_time ON injury_reads (fetched_at)",
    ),
    # d05eb1a added NOT NULL to these two columns without a migration step, so a store that
    # predates it accepts a row that a fresh store rejects. Both columns are written by every
    # writer, so no stored row holds a NULL and the rebuild copies every row.
    4: (
        _SOURCE_READS_V5,
        "INSERT INTO source_reads_v5 (source, url, fetched_at, ok, error)"
        " SELECT source, url, fetched_at, ok, error FROM source_reads",
        "DROP TABLE source_reads",
        "ALTER TABLE source_reads_v5 RENAME TO source_reads",
        _MODEL_FITS_V5,
        "INSERT INTO model_fits_v5 (fit_id, fit_through_season, features, weights, created_at)"
        " SELECT fit_id, fit_through_season, features, weights, created_at FROM model_fits",
        "DROP TABLE model_fits",
        "ALTER TABLE model_fits_v5 RENAME TO model_fits",
    ),
    # A row that an earlier version wrote keeps NULL, which means that the cause of the
    # failure is unknown. The reader counts it as transient.
    5: ("ALTER TABLE source_reads ADD COLUMN permanent INTEGER",),
    # No code wrote or read starter_reads.nhl_id or injury_reads.nhl_id (#73). A value that a
    # store holds there is copied to retired_nhl_ids, with every other column of its row,
    # before the column is dropped. A store that holds no value gets an empty archive table.
    6: (
        _RETIRED_NHL_IDS,
        "INSERT INTO retired_nhl_ids (source_table, observation, fetched_at, nhl_id)"
        " SELECT 'starter_reads', json_array(game_id, team, source, goalie_name, status),"
        " fetched_at, nhl_id FROM starter_reads WHERE nhl_id IS NOT NULL",
        "INSERT INTO retired_nhl_ids (source_table, observation, fetched_at, nhl_id)"
        " SELECT 'injury_reads', json_array(team, player_name, status, return_date),"
        " fetched_at, nhl_id FROM injury_reads WHERE nhl_id IS NOT NULL",
        "ALTER TABLE starter_reads DROP COLUMN nhl_id",
        "ALTER TABLE injury_reads DROP COLUMN nhl_id",
    ),
    # No feature read the unblocked counts (#92). The archive copies each stored value with the
    # key of its row before the rebuild drops the columns.
    7: (
        _RETIRED_MP_UNBLOCKED,
        "INSERT OR IGNORE INTO retired_mp_unblocked (game_id, team, fetched_at,"
        " unblocked_for,"
        " unblocked_against) SELECT game_id, team, fetched_at, unblocked_for, unblocked_against"
        " FROM mp_team_games WHERE unblocked_for IS NOT NULL OR unblocked_against IS NOT NULL",
        _MP_TEAM_GAMES_V8,
        "INSERT INTO mp_team_games_v8 (game_id, team, game_date, xg_for, xg_against,"
        " shot_attempts_for, shot_attempts_against, attempts_known, fetched_at)"
        " SELECT game_id, team, game_date, xg_for, xg_against, shot_attempts_for,"
        " shot_attempts_against,"
        " CASE WHEN shot_attempts_for IS NOT NULL AND shot_attempts_against IS NOT NULL"
        " THEN 1 ELSE 0 END, fetched_at FROM mp_team_games",
        "DROP TABLE mp_team_games",
        "ALTER TABLE mp_team_games_v8 RENAME TO mp_team_games",
        "CREATE INDEX IF NOT EXISTS mp_team_games_team ON mp_team_games (team)",
    ),
}
# d05eb1a added NOT NULL to these two columns. A row that an earlier store wrote can hold a
# NULL there, and the rebuild cannot invent a value, so the step stops before it copies one.
NULLABLE_BEFORE_V5: tuple[tuple[str, str], ...] = (
    ("source_reads", "fetched_at"),
    ("model_fits", "created_at"),
)
