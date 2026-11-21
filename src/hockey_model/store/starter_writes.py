"""Write starting goalie reads to the store."""

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import NamedTuple

from hockey_model.sources.starters import StarterRead
from hockey_model.store.db import StoreError
from hockey_model.store.reads import franchise_codes
from hockey_model.store.sql_text import placeholders
from hockey_model.store.timestamps import iso_utc

# A source and the NHL feed can differ on a start time after a schedule change.
START_TOLERANCE = timedelta(hours=12)


class StarterWrite(NamedTuple):
    """How many reads were stored, and which reads matched no stored game."""

    written: int
    unmatched: tuple[str, ...]


def _find_game(conn: sqlite3.Connection, read: StarterRead) -> int | None:
    """Return the game of ``read``, or ``None`` when the store has no such game.

    A read with two candidate games is an error: it must not guess between them.
    """
    codes = franchise_codes(read.team)
    marks = placeholders(codes)
    low = iso_utc(read.start - START_TOLERANCE)
    high = iso_utc(read.start + START_TOLERANCE)
    rows = conn.execute(
        "SELECT DISTINCT game_id FROM latest_games"
        f" WHERE (home IN ({marks}) OR away IN ({marks})) AND start_utc BETWEEN ? AND ?",
        (*codes, *codes, low, high),
    ).fetchall()
    if len(rows) > 1:
        raise StoreError(
            f"match {read.source} goalie read for {read.team} at {read.start.isoformat()}: "
            f"{len(rows)} stored games fit; fix the stored schedule"
        )
    return rows[0][0] if rows else None


def write_starter_reads(
    conn: sqlite3.Connection, reads: Sequence[StarterRead], fetched_at: datetime
) -> StarterWrite:
    """Append each read that matches a stored game. Report the reads that match none."""
    stamp = iso_utc(fetched_at)
    written = 0
    unmatched: list[str] = []
    for read in reads:
        game_id = _find_game(conn, read)
        if game_id is None:
            unmatched.append(f"{read.team} {read.start.isoformat()}")
            continue
        conn.execute(
            "INSERT INTO starter_reads (game_id, team, source, goalie_name, status, fetched_at)"
            " VALUES (?,?,?,?,?,?)",
            (game_id, read.team, read.source.value, read.goalie_name, read.status.value, stamp),
        )
        written += 1
    return StarterWrite(written, tuple(unmatched))
