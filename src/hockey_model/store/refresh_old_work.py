"""Tell a backfill that the budget stopped before the backlog from one with recent work only."""

from datetime import date, timedelta

from hockey_model.sources.nhl import boxscore_url, score_day_url
from hockey_model.store.nhl_writes import games_missing_boxscore
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.refresh_nhl import (
    BOXSCORES,
    RETRY_FAILED_AFTER,
    SCORES,
    day_is_complete,
)
from hockey_model.store.source_reads import failed_since, last_ok_reads


def old_work_left(run: RefreshRun, first: date, cutoff: date, gaps: set[date]) -> bool:
    """Return True when a score day or a boxscore older than ``cutoff`` still waits for a read.

    A score day in ``gaps`` and a boxscore that is a known gap or that failed a short time ago
    do not wait, because the run skips them. A run that stopped on the budget with no such work
    left has only recent days to finish, and the next run finishes them.
    """
    score_reads = last_ok_reads(run.conn, SCORES)
    day = first
    while day < cutoff:
        last = score_reads.get(score_day_url(day))
        if day not in gaps and not day_is_complete(day, last):
            return True
        day += timedelta(days=1)
    known_gaps = run.skipped(BOXSCORES)
    retry_after = run.now - RETRY_FAILED_AFTER
    for game_id in games_missing_boxscore(run.conn, first, cutoff - timedelta(days=1)):
        url = boxscore_url(game_id)
        if game_id in run.boxscores_read or url in known_gaps:
            continue
        if not failed_since(run.conn, BOXSCORES, url, retry_after):
            return True
    return False
