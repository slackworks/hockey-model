"""Keep the status of a game in step with the unit that wrote one of its inputs (#35, #367).

A refresh unit calls this in its derive hook, so the status and the input change in one
transaction. Design: docs/design/pbp-shift-dataset.md
"""

import sqlite3

from hockey_model.store.db import StoreError
from hockey_model.store.reconcile_game import reconcile_game
from hockey_model.store.refresh_context import RefreshRun


def reconcile_in_unit(run: RefreshRun, conn: sqlite3.Connection, game_id: int) -> None:
    """Reconcile one game inside the transaction of the running unit.

    A game that cannot reconcile has its stints and status removed by ``reconcile_game``, so its
    status is unknown. The error goes to the report. It does not fail the unit, because the bytes
    that the unit read are good and the archive must keep them.
    """
    try:
        reconcile_game(conn, game_id, run.last_stamp or run.now)
    except StoreError as exc:
        run.errors.append(exc)
