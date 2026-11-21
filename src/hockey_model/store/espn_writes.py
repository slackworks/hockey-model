"""Write ESPN injury reads to the store."""

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from hockey_model.sources.espn import InjuryRead
from hockey_model.store.timestamps import iso_utc


def write_injury_reads(
    conn: sqlite3.Connection, reads: Sequence[InjuryRead], fetched_at: datetime
) -> int:
    """Append one injury read for each listed player. The ESPN list has no history."""
    stamp = iso_utc(fetched_at)
    conn.executemany(
        "INSERT INTO injury_reads (team, player_name, status, return_date, fetched_at)"
        " VALUES (?,?,?,?,?)",
        [
            (
                read.team,
                read.player_name,
                read.status,
                None if read.return_date is None else read.return_date.isoformat(),
                stamp,
            )
            for read in reads
        ],
    )
    return len(reads)
