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

TEAMS = ("TOR", "BOS")
GAME_COUNT = 2


def seed(path: Path) -> None:
    """Write the two games of the smoke store to ``path``."""
    conn = connect(path)
    try:
        rows = [
            GameRow(
                2015020001,
                20152016,
                2,
                datetime(2015, 11, 1, tzinfo=UTC),
                *TEAMS,
                "OFF",
                3,
                2,
                "REG",
            ),
            GameRow(
                2025020001,
                20262027,
                2,
                datetime(2026, 12, 1, tzinfo=UTC),
                *TEAMS,
                "FUT",
                None,
                None,
                None,
            ),
        ]
        write_games(conn, rows, datetime(2026, 11, 1, tzinfo=UTC))
        conn.commit()
        stored = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    finally:
        conn.close()
    if stored != GAME_COUNT:
        raise SystemExit(f"seed {path}: expected {GAME_COUNT} games in the store, found {stored}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("smoke_seed: pass one argument, the path of the store file to create")
    seed(Path(sys.argv[1]))
