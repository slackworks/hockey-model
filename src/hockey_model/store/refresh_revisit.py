"""The revisit steps of a refresh: read final games again, to keep each correction (#82, #35)."""

from collections.abc import Callable, Sequence

from hockey_model.sources.nhl import boxscore_url
from hockey_model.store.boxscore_revisit import REVISIT_AFTER_DAYS, boxscores_due, reads_due
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.refresh_nhl import (
    BOXSCORES,
    MAX_REVISITS,
    read_boxscores,
)


def revisit_reads(
    run: RefreshRun,
    source: str,
    due_games: Sequence[int],
    url_of: Callable[[int], str],
    read: Callable[[list[int], list[int]], object],
) -> None:
    """Read each game of ``due_games`` again: the oldest due first, and bounded.

    A revisit follows the rules of a first read: one transaction, a stop after three failures,
    and a wait after a failed read. A game left out by the bound is named in the notes, and the
    next refresh reads it. ``read`` takes the games to read and all the due games.
    """
    gaps = run.skipped(source)
    # A known gap would use a slot of the bound and then be dropped, so the bound counts only the
    # games that a read can reach.
    due = [game_id for game_id in due_games if url_of(game_id) not in gaps]
    if len(due) > MAX_REVISITS:
        run.notes.append(
            f"{source}: {len(due) - MAX_REVISITS} revisit(s) wait for the next "
            f"refresh; this run reads at most {MAX_REVISITS}"
        )
    with run.step(f"{source}-revisits", "due", min(len(due), MAX_REVISITS)):
        read(due[:MAX_REVISITS], due)


def revisit_boxscores(run: RefreshRun) -> None:
    """Read each final boxscore that is due again."""
    due = boxscores_due(run.conn, run.now, REVISIT_AFTER_DAYS)
    revisit_reads(
        run, BOXSCORES, due, boxscore_url, lambda games, recent: read_boxscores(run, games, recent)
    )


def revisit_source(
    run: RefreshRun,
    source: str,
    url_of: Callable[[int], str],
    read: Callable[[list[int]], object],
) -> None:
    """Read each final game that is due again for an event source."""
    due = reads_due(run.conn, run.now, REVISIT_AFTER_DAYS, source)
    revisit_reads(run, source, due, url_of, lambda games, _recent: read(games))
