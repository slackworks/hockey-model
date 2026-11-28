"""The unit that reads one game of an event source and reconciles the game (#35).

The play-by-play, the shift chart, and the ESPN summary share this unit. The derive hook writes
the rows of the observation, and then it reconciles the game in the same transaction. A failed
unit reconciles too, because a refused observation changes the status of the game.
Design: docs/design/pbp-shift-dataset.md
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from hockey_model.sources.http import HttpClient
from hockey_model.sources.shape_errors import SourcePendingError
from hockey_model.store.db import StoreError
from hockey_model.store.recording import Reading
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.source_reads import ESPN_SUMMARY

Writer = Callable[[sqlite3.Connection, int], None]


def event_unit(
    run: RefreshRun,
    source: str,
    game_id: int,
    parser: str,
    fetch: Callable[[HttpClient], Writer],
) -> tuple[Callable[[datetime], int], Reading]:
    """Return the unit that reads one game of ``source``, and how its bytes are archived.

    ``fetch`` makes the one request and returns the writer of the parsed rows. A feed that is
    not published yet adds one to ``run.unpublished``, so the caller does not count the read as
    a failure.
    """
    held: list[Writer] = []

    def unit(_stamp: datetime) -> int:
        held.clear()
        held.append(fetch(run.client))
        return 1

    def derive(conn: sqlite3.Connection, obs_id: int) -> None:
        if len(held) != 1:
            raise StoreError(
                f"derive {source} {game_id}: the unit ended with no parsed rows; "
                "the derive hook runs only after a good unit"
            )
        held[0](conn, obs_id)
        run.reconcile_in_unit(game_id)

    def not_yet(failure: Exception) -> str | None:
        """Say that a feed is not published yet. Such a read never counts toward a known gap."""
        if isinstance(failure, SourcePendingError):
            run.unpublished += 1
            return f"{source}: game {game_id}: {failure}; the next refresh reads it again"
        return None

    # The URL of an ESPN summary names the ESPN event, so its key is the NHL game id.
    key = str(game_id) if source == ESPN_SUMMARY else None
    return unit, Reading(
        parser,
        key=key,
        expected_absence=not_yet,
        derive=derive,
        on_failure=lambda _conn: run.reconcile_in_unit(game_id),
    )
