"""Seed a small store for the wheel smoke test (#98).

Usage: python -I smoke_seed.py <store.db>

The smoke harness runs this file with the Python of the clean venv, so it uses the installed
package. It stores one finished game and one scheduled game, with no other history.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

from hockey_model.sources.nhl import GameRow
from hockey_model.store.db import connect
from hockey_model.store.nhl_writes import write_games

FINISHED_ID = 2015020001
SCHEDULED_ID = 2025020001


def _start(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def seed(path: Path) -> None:
    """Write the two games of the smoke store to ``path``."""
    conn = connect(path)
    try:
        rows = [
            GameRow(
                FINISHED_ID,
                20152016,
                2,
                _start("2015-11-01T00:00"),
                "TOR",
                "BOS",
                "OFF",
                3,
                2,
                "REG",
            ),
            GameRow(
                SCHEDULED_ID,
                20262027,
                2,
                _start("2026-12-01T00:00"),
                "TOR",
                "BOS",
                "FUT",
                None,
                None,
                None,
            ),
        ]
        write_games(conn, rows, datetime(2026, 11, 1, tzinfo=UTC))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("smoke_seed: pass one argument, the path of the store file to create")
    seed(Path(sys.argv[1]))
