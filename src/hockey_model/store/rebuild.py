"""Rebuild the rows that one source derives from its archived payloads (#82)."""

import sqlite3

from hockey_model.store.boxscore_rebuild import RebuildReport, rebuild_boxscores
from hockey_model.store.db import StoreError
from hockey_model.store.event_rebuild import KINDS, rebuild_events
from hockey_model.store.source_reads import BOXSCORES

# The sources that a rebuild supports. A source with no derived rows has nothing to rebuild.
REBUILDABLE = (BOXSCORES, *KINDS)


def rebuild_source(
    conn: sqlite3.Connection, source: str, parser: str, *, allow_refused: bool = False
) -> RebuildReport:
    """Parse the archived payloads of ``source`` again with ``parser``, and report what changed.

    The command makes no network request. Raises ``StoreError`` for a source that has no derived
    rows to rebuild, and for a parser that is not the one the code holds.
    """
    if source == BOXSCORES:
        return rebuild_boxscores(conn, parser, allow_refused=allow_refused)
    if source in KINDS:
        return rebuild_events(conn, source, parser, allow_refused=allow_refused)
    raise StoreError(
        f"rebuild {source!r}: this source has no rows to rebuild; "
        f"pass --source one of {', '.join(REBUILDABLE)}"
    )
