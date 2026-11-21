"""As-of reads: what the store knew at a time.

A game result is known once the game is over. The store does not keep the end time, so a
result counts as known ``RESULT_DELAY`` after the start. Starter and injury reads are known
from their fetch time. The result tables have no fetch-time limit: a backfill fetches old
games late, and the backtest must still see them.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from hockey_model.game import StarterSource, StarterStatus
from hockey_model.game_day import game_day_window
from hockey_model.identity import Catalog
from hockey_model.sources.espn import InjuryRead
from hockey_model.sources.nhl import FINAL_STATES, GameRow
from hockey_model.sources.teams import franchise
from hockey_model.store.source_reads import ESPN_INJURIES, LIVE_FEED_MAX_AGE
from hockey_model.store.sql_text import placeholders
from hockey_model.store.timestamps import iso_utc, parse_iso_utc
from hockey_model.team_codes import NHL_TEAM_CODES
from hockey_model.types import Player, Slot

# An NHL game ends about three hours after the puck drops, with overtime and a shootout.
RESULT_DELAY = timedelta(hours=3)
# Later than any game, so a read returns every final game in the store.
LATEST_GAME = datetime(9000, 1, 1, tzinfo=UTC)
_POSITION_SLOTS = {"C": Slot.C, "L": Slot.W, "R": Slot.W, "D": Slot.D, "G": Slot.G}


@dataclass(frozen=True, slots=True)
class StarterReadRow:
    """One stored goalie read."""

    source: StarterSource
    goalie_name: str
    nhl_id: int | None
    status: StarterStatus
    fetched_at: datetime


def franchise_codes(team: str) -> tuple[str, ...]:
    """Return every NHL code that the franchise of ``team`` has used."""
    home = franchise(team)
    return tuple(sorted(code for code in NHL_TEAM_CODES if franchise(code) == home))


def result_cutoff(before: datetime) -> str:
    """Return the latest start of a game that was over by ``before``."""
    return iso_utc(before - RESULT_DELAY)


def _game_row(row: Sequence[Any]) -> GameRow:
    game_id, season, game_type, start, home, away, state, home_score, away_score, last = row
    return GameRow(
        game_id,
        season,
        game_type,
        parse_iso_utc(start),
        home,
        away,
        state,
        home_score,
        away_score,
        last,
    )


_GAME_COLUMNS = (
    "g.game_id, g.season, g.game_type, g.start_utc, g.home, g.away, g.state,"
    " g.home_score, g.away_score, g.last_period"
)


def final_games_before(conn: sqlite3.Connection, before: datetime) -> list[GameRow]:
    """Return the games that were over at ``before``, oldest first."""
    cursor = conn.execute(
        f"SELECT {_GAME_COLUMNS} FROM latest_final_games g WHERE g.start_utc <= ?"
        " ORDER BY g.start_utc, g.game_id",
        (result_cutoff(before),),
    )
    return [_game_row(row) for row in cursor]


def team_games_before(
    conn: sqlite3.Connection, team: str, before: datetime, limit: int
) -> list[GameRow]:
    """Return the newest games of the franchise of ``team`` that were over at ``before``."""
    codes = franchise_codes(team)
    cursor = conn.execute(
        f"SELECT {_GAME_COLUMNS} FROM latest_final_games g WHERE g.start_utc <= ?"
        f" AND (g.home IN ({placeholders(codes)}) OR g.away IN ({placeholders(codes)}))"
        " ORDER BY g.start_utc DESC, g.game_id DESC LIMIT ?",
        (result_cutoff(before), *codes, *codes, limit),
    )
    return [_game_row(row) for row in cursor]


def goalie_starts_before(
    conn: sqlite3.Connection, team: str, before: datetime, limit: int
) -> list[tuple[int, int]]:
    """Return ``(game_id, nhl_id)`` of the starter in the newest games of the franchise."""
    codes = franchise_codes(team)
    cursor = conn.execute(
        f"SELECT g.game_id, w.nhl_id FROM latest_final_games g"
        " JOIN goalie_games w ON w.game_id = g.game_id"
        f" WHERE w.started = 1 AND w.team IN ({placeholders(codes)}) AND g.start_utc <= ?"
        " ORDER BY g.start_utc DESC, g.game_id DESC LIMIT ?",
        (*codes, result_cutoff(before), limit),
    )
    return [(row[0], row[1]) for row in cursor]


def mp_team_form_before(
    conn: sqlite3.Connection, team: str, before: datetime, limit: int
) -> tuple[list[tuple[float, float]], int]:
    """Return the ``(xg_for, xg_against)`` pairs of the newest MoneyPuck games of the
    franchise, and how many of those newest games a NULL column left out."""
    return _mp_pairs_before(conn, ("xg_for", "xg_against"), team, before, limit)


_MP_PAIR_COLUMNS = frozenset({"xg_for", "xg_against", "shot_attempts_for", "shot_attempts_against"})


def _mp_pairs_before(
    conn: sqlite3.Connection, columns: tuple[str, str], team: str, before: datetime, limit: int
) -> tuple[list[tuple[float, float]], int]:
    """Return two named MoneyPuck team-game columns of the newest games of the franchise,
    and how many of those newest games a NULL column left out of the pairs.

    The names are placed in the SQL, so only the known columns are accepted.
    """
    unknown = [name for name in columns if name not in _MP_PAIR_COLUMNS]
    if unknown:
        raise ValueError(
            f"read MoneyPuck form: unknown column {unknown[0]!r}; "
            f"use one of {sorted(_MP_PAIR_COLUMNS)}"
        )
    codes = franchise_codes(team)
    cursor = conn.execute(
        f"SELECT m.{columns[0]}, m.{columns[1]} FROM latest_mp_team_games m"
        " JOIN latest_final_games g ON g.game_id = m.game_id"
        f" WHERE m.team IN ({placeholders(codes)}) AND g.start_utc <= ?"
        " ORDER BY g.start_utc DESC, g.game_id DESC LIMIT ?",
        (*codes, result_cutoff(before), limit),
    )
    pairs: list[tuple[float, float]] = []
    left_out = 0
    for row in cursor:
        # A NULL is an unknown value, such as a version 1 row has for attempt counts.
        if row[0] is None or row[1] is None:
            left_out += 1
        else:
            pairs.append((row[0], row[1]))
    return pairs, left_out


def mp_goalie_form_before(
    conn: sqlite3.Connection, nhl_id: int, before: datetime, limit: int
) -> list[tuple[float, int]]:
    """Return ``(xg_against, goals_against)`` of the newest MoneyPuck games of a goalie."""
    cursor = conn.execute(
        "SELECT m.xg_against, m.goals_against FROM latest_mp_goalie_games m"
        " JOIN latest_final_games g ON g.game_id = m.game_id"
        " WHERE m.nhl_id = ? AND g.start_utc <= ?"
        " ORDER BY g.start_utc DESC, g.game_id DESC LIMIT ?",
        (nhl_id, result_cutoff(before), limit),
    )
    return [(row[0], row[1]) for row in cursor]


def starter_reads_before(
    conn: sqlite3.Connection, game_id: int, team: str, before: datetime
) -> list[StarterReadRow]:
    """Return the goalie reads for one team of one game that were fetched before ``before``."""
    cursor = conn.execute(
        "SELECT source, goalie_name, nhl_id, status, fetched_at FROM starter_reads"
        " WHERE game_id = ? AND team = ? AND fetched_at < ? ORDER BY fetched_at, rowid",
        (game_id, team, iso_utc(before)),
    )
    return [
        StarterReadRow(
            StarterSource(source), name, nhl_id, StarterStatus(status), parse_iso_utc(stamp)
        )
        for source, name, nhl_id, status, stamp in cursor
    ]


def unresolved_games(
    conn: sqlite3.Connection, started_by: datetime
) -> list[tuple[int, int, int, datetime]]:
    """Return ``(game_id, season, game_type, start)`` of each non-final game started by a time.

    The newest read of the game decides. The list is in start then id order.
    """
    marks = placeholders(tuple(sorted(FINAL_STATES)))
    cursor = conn.execute(
        "SELECT game_id, season, game_type, start_utc FROM latest_games"
        f" WHERE state NOT IN ({marks}) AND start_utc <= ? ORDER BY start_utc, game_id",
        (*sorted(FINAL_STATES), iso_utc(started_by)),
    )
    return [(gid, season, kind, parse_iso_utc(start)) for gid, season, kind, start in cursor]


def injury_snapshot_before(conn: sqlite3.Connection, before: datetime) -> datetime | None:
    """Return the time of the newest good read of the injury list before ``before``."""
    (stamp,) = conn.execute(
        "SELECT MAX(fetched_at) FROM source_reads WHERE source = ? AND ok = 1 AND fetched_at < ?",
        (ESPN_INJURIES, iso_utc(before)),
    ).fetchone()
    return None if stamp is None else parse_iso_utc(stamp)


def injury_reads_before(conn: sqlite3.Connection, before: datetime) -> list[InjuryRead]:
    """Return the newest injury list that was read before ``before``.

    A good read of the list is a snapshot even when the list is empty, so players who
    healed drop out. The snapshot time comes from ``source_reads``. A snapshot older than
    ``LIVE_FEED_MAX_AGE`` is not used: it could still name a player who has healed, and
    ``stale_sources`` reports it.
    """
    stamp = injury_snapshot_before(conn, before)
    if stamp is None or before - stamp > LIVE_FEED_MAX_AGE:
        return []
    stamp = iso_utc(stamp)
    cursor = conn.execute(
        "SELECT team, player_name, status, return_date FROM injury_reads WHERE fetched_at = ?",
        (stamp,),
    )
    return [
        InjuryRead(team, name, status, None if day is None else date.fromisoformat(day))
        for team, name, status, day in cursor
    ]


def player_catalog(conn: sqlite3.Connection) -> Catalog:
    """Return a catalog with one player for each NHL id, from the newest boxscore row."""
    cursor = conn.execute(
        "SELECT nhl_id, full_name, position, team FROM latest_players ORDER BY nhl_id"
    )
    players = [
        Player(
            f"nhl:{nhl_id}",
            None,
            nhl_id,
            name,
            frozenset({_POSITION_SLOTS[position]}) if position in _POSITION_SLOTS else None,
            team,
        )
        for nhl_id, name, position, team in cursor
    ]
    return Catalog(players)


def game_row(conn: sqlite3.Connection, game_id: int) -> GameRow | None:
    """Return the newest read of one game in any state, or ``None`` when it is not stored."""
    row = conn.execute(
        f"SELECT {_GAME_COLUMNS} FROM games g WHERE g.game_id = ?"
        " ORDER BY g.fetched_at DESC LIMIT 1",
        (game_id,),
    ).fetchone()
    return None if row is None else _game_row(row)


def mp_attempt_form_before(
    conn: sqlite3.Connection, team: str, before: datetime, limit: int
) -> tuple[list[tuple[float, float]], int]:
    """Return the ``(shot attempts for, against)`` pairs of the newest MoneyPuck games of the
    franchise, and how many of those newest games a NULL column left out."""
    return _mp_pairs_before(
        conn, ("shot_attempts_for", "shot_attempts_against"), team, before, limit
    )


def games_on_day(conn: sqlite3.Connection, day: date) -> list[GameRow]:
    """Return the newest read of each game of the Eastern game ``day``, earliest first.

    The newest read can be in progress. The caller decides what a state means.
    """
    opens, closes = game_day_window(day)
    cursor = conn.execute(
        f"SELECT {_GAME_COLUMNS} FROM latest_games g WHERE g.start_utc >= ? AND g.start_utc < ?"
        " ORDER BY g.start_utc, g.game_id",
        (iso_utc(opens), iso_utc(closes)),
    )
    return [_game_row(row) for row in cursor]
