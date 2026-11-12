"""The per-game predictions of a backtest, and their CSV export."""

import csv
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

from hockey_model.store.timestamps import iso_utc


@dataclass(frozen=True, slots=True)
class GameRecord:
    """One predicted game, for scoring against a line outside hockey-model."""

    game_id: int
    start: datetime
    home: str
    away: str
    run: str
    home_win_prob: float
    home_won: bool


def write_game_records(path: Path, records: Sequence[GameRecord]) -> None:
    """Write one CSV row for each record. The ``start`` column is ISO 8601 UTC."""
    names = [f.name for f in fields(GameRecord)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(names)
        for record in records:
            writer.writerow(
                [
                    iso_utc(record.start) if name == "start" else getattr(record, name)
                    for name in names
                ]
            )
