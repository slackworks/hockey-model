"""Choose the final games whose read of a source a refresh does again (#82, #35).

A source can correct a boxscore, a play-by-play, or a shift chart after the game. A game is read
again at each revisit day after its start, as long as no read came after that day.
Design: docs/design/payload-archive.md
"""

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta

from hockey_model.store.db import StoreError
from hockey_model.store.source_reads import BOXSCORES
from hockey_model.store.timestamps import parse_iso_utc
from hockey_model.types import HockeyModelError

# A final game is read again this many days after its start, when no read has come since. The
# NHL corrects a game after the final horn, and each correction is a new revision.
REVISIT_AFTER_DAYS = (2, 14)


def _due_at(start: datetime, newest_read: datetime, after_days: Sequence[int]) -> datetime | None:
    """Return the first revisit time that no read has met yet, or None when every one is met."""
    for days in sorted(after_days):
        point = start + timedelta(days=days)
        if newest_read < point:
            return point
    return None


def _times(game_id: int, start: str, newest: str) -> tuple[datetime, datetime]:
    """Return the start of the game and the time of its newest good read, as times."""
    try:
        return parse_iso_utc(start), parse_iso_utc(newest)
    except (ValueError, HockeyModelError) as exc:
        raise StoreError(
            f"read the revisit times of game {game_id}: start {start!r} or read {newest!r} is "
            f"not a stored UTC time ({exc}); repair the row in games or observations"
        ) from exc


def reads_due(
    conn: sqlite3.Connection, now: datetime, after_days: Sequence[int], source: str
) -> list[int]:
    """Return the final games whose read of ``source`` is due for a revisit, oldest due first.

    A game is due when a revisit time has passed and the newest good read of its ``source`` is
    older than that time. A game with no good read is not a revisit: the first read covers it.
    """
    cursor = conn.execute(
        "SELECT g.game_id, g.start_utc, MAX(o.fetched_at) FROM latest_final_games g"
        " JOIN observations o ON o.source = ? AND o.key = CAST(g.game_id AS TEXT)"
        " AND o.status = 'ok' GROUP BY g.game_id, g.start_utc",
        (source,),
    )
    due: list[tuple[datetime, int]] = []
    for game_id, start, newest in cursor:
        point = _due_at(*_times(int(game_id), start, newest), after_days)
        if point is not None and point <= now:
            due.append((point, int(game_id)))
    return [game_id for _, game_id in sorted(due)]


def boxscores_due(conn: sqlite3.Connection, now: datetime, after_days: Sequence[int]) -> list[int]:
    """Return the final games whose boxscore is due for a revisit, the oldest due time first."""
    return reads_due(conn, now, after_days, BOXSCORES)
