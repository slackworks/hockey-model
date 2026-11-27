"""The revisit step of a refresh: read final boxscores again, to keep each correction (#82)."""

from hockey_model.store.boxscore_revisit import boxscores_due
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.refresh_nhl import (
    BOXSCORES,
    MAX_REVISITS,
    REVISIT_AFTER_DAYS,
    read_boxscores,
)


def revisit_boxscores(run: RefreshRun) -> None:
    """Read each final boxscore that is due again, the oldest due first and bounded.

    A revisit follows the rules of a first read: one transaction, a stop after three failures,
    and a wait after a failed read. A game left out by the bound is named in the notes, and the
    next refresh reads it.
    """
    due = boxscores_due(run.conn, run.now, REVISIT_AFTER_DAYS)
    if len(due) > MAX_REVISITS:
        run.notes.append(
            f"{BOXSCORES}: {len(due) - MAX_REVISITS} boxscore revisit(s) wait for the next "
            f"refresh; this run reads at most {MAX_REVISITS}"
        )
    with run.step("boxscore-revisits", "due", min(len(due), MAX_REVISITS)):
        read_boxscores(run, due[:MAX_REVISITS], due)
