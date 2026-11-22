"""The key of the cached rows of one season.

The rows of a season read the final games before each of its games, the MoneyPuck rows of those
games, the boxscore goalie rows, the injury reads before each game, and the goalies of the
player catalog. They also start from the Elo ratings before the season, which every earlier game
moves. The key of a season is a running hash over all of this data from the first game through
the start of the last game of that season, with the data of the catalog, the features, the
burn-in season, and the source files of the package. A change to an old game therefore changes
the key of every later season, and a new game changes the key of its own season only. A
change to the goalie catalog changes every key.
Design: docs/design/backtest-speed.md
"""

import hashlib
import json
import sqlite3
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import hockey_model
from hockey_model.store.source_reads import ESPN_INJURIES

# Bump this when the recipe of the key changes, so no old cache file is read again.
_RECIPE = "season-rows-1"

# Every table or view that a feature build reads. ``test_a_build_reads_only_tables_that_the_key_
# covers`` fails when a build reads another one.
COVERED_TABLES = (
    "latest_final_games",
    "latest_mp_team_games",
    "latest_mp_goalie_games",
    "goalie_games",
    "latest_players",
    "source_reads",
    "injury_reads",
)

# The first column of a season query is the season, and a row sorts by game start.
_GAMES = "SELECT g.season, g.* FROM latest_final_games g ORDER BY g.season, g.start_utc, g.game_id"
_MP_TEAM = (
    "SELECT g.season, m.* FROM latest_mp_team_games m"
    " JOIN latest_final_games g ON g.game_id = m.game_id"
    " ORDER BY g.season, g.start_utc, m.game_id, m.team"
)
_MP_GOALIE = (
    "SELECT g.season, m.* FROM latest_mp_goalie_games m"
    " JOIN latest_final_games g ON g.game_id = m.game_id"
    " ORDER BY g.season, g.start_utc, m.game_id, m.nhl_id"
)
_GOALIE_GAMES = (
    "SELECT g.season, w.* FROM goalie_games w"
    " JOIN latest_final_games g ON g.game_id = w.game_id"
    " ORDER BY g.season, g.start_utc, w.game_id, w.nhl_id"
)

# A read of the injury list that a game can use was fetched before the game started, so the
# reads count for the season whose last game starts at or after the read. A read after the last
# game of the newest season reaches no row.
_INJURY_STAMPS = (
    f"SELECT fetched_at FROM source_reads WHERE source = '{ESPN_INJURIES}' AND ok = 1"
    " ORDER BY fetched_at"
)
_INJURIES = (
    "SELECT fetched_at, team, player_name, status, return_date FROM injury_reads"
    " ORDER BY fetched_at, team, player_name, status, return_date"
)
_SEASON_ENDS = (
    "SELECT MAX(start_utc), season FROM latest_final_games GROUP BY season ORDER BY season"
)

# A catalog goalie, and any player who started a game in goal. The newest row of a player is
# the one that a build reads, and a build reads the team of the player now, not at the game.
_CATALOG = (
    "SELECT p.* FROM latest_players p WHERE p.position = 'G'"
    " OR p.nhl_id IN (SELECT nhl_id FROM goalie_games) ORDER BY p.nhl_id"
)

# The time of a read does not reach a row, and a re-read of an old day writes it again.
_READ_TIME = "fetched_at"

# The hash of a season that a table has no row for, so each table keeps its place in the chain.
_NO_ROWS = bytes(hashlib.sha256().digest_size)


def source_digest(root: Path) -> str:
    """Return a hash of the Python and SQL files under ``root``, by path and content."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.suffix in {".py", ".sql"}):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@cache
def source_fingerprint() -> str:
    """Return the hash of the source files of the installed package."""
    return source_digest(Path(hockey_model.__file__).parent)


def _row_text(row: Sequence[object], skip: int | None) -> bytes:
    return repr(tuple(v for i, v in enumerate(row) if i != skip)).encode()


def _read_time_column(cursor: sqlite3.Cursor) -> int:
    return [column[0] for column in cursor.description].index(_READ_TIME)


def _digest_by_season(pairs: Iterable[tuple[int, bytes]]) -> dict[int, bytes]:
    digests: dict[int, Any] = {}
    for season, text in pairs:
        digests.setdefault(season, hashlib.sha256()).update(text)
    return {season: digest.digest() for season, digest in digests.items()}


def _season_digests(
    conn: sqlite3.Connection, query: str, *, ignore_read_time: bool = True
) -> dict[int, bytes]:
    """Return a hash of the rows of each season. The first column of a row is its season."""
    cursor = conn.execute(query)
    skip = _read_time_column(cursor) if ignore_read_time else None
    return _digest_by_season((row[0], _row_text(row, skip)) for row in cursor)


def _digests_by_time(conn: sqlite3.Connection, query: str) -> dict[int, bytes]:
    """Return a hash of the rows that each season can read, by the time of the read.

    The first column of a row is its fetch time, and the rows come in that order.
    """
    ends = conn.execute(_SEASON_ENDS).fetchall()
    stops = [end for end, _ in ends]

    def season_of(stamp: str) -> int | None:
        index = bisect_left(stops, stamp)
        return ends[index][1] if index < len(ends) else None

    rows = [(season_of(row[0]), _row_text(row, None)) for row in conn.execute(query)]
    return _digest_by_season((season, text) for season, text in rows if season is not None)


def _catalog_digest(conn: sqlite3.Connection) -> bytes:
    cursor = conn.execute(_CATALOG)
    skip = _read_time_column(cursor)
    digest = hashlib.sha256()
    for row in cursor:
        digest.update(_row_text(row, skip))
    return digest.digest()


def season_keys(conn: sqlite3.Connection, burn_in: int, names: Sequence[str]) -> dict[int, str]:
    """Return the key of each season that holds a final game.

    The key depends on the data through the end of its season, the goalie catalog, ``names``
    in their order, ``burn_in``, and the source files of the package.
    """
    tables = [
        _season_digests(conn, _GAMES),
        _season_digests(conn, _MP_TEAM),
        _season_digests(conn, _MP_GOALIE),
        # A boxscore goalie row is written once, so its read time never moves.
        _season_digests(conn, _GOALIE_GAMES, ignore_read_time=False),
        _digests_by_time(conn, _INJURY_STAMPS),
        _digests_by_time(conn, _INJURIES),
    ]
    catalog = _catalog_digest(conn)
    params = json.dumps(
        {"recipe": _RECIPE, "burn_in": burn_in, "names": list(names), "code": source_fingerprint()}
    )
    keys: dict[int, str] = {}
    running = hashlib.sha256()
    for season in sorted(tables[0]):
        running.update(str(season).encode())
        for table in tables:
            running.update(table.get(season, _NO_ROWS))
        keys[season] = hashlib.sha256(
            running.digest() + catalog + params.encode() + str(season).encode()
        ).hexdigest()
    return keys
