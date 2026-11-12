"""Fill the store from every source. The prediction call reads the store and fetches nothing."""

import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta

from hockey_model.sources.http import HttpClient
from hockey_model.store.refresh_backfill import nhl
from hockey_model.store.refresh_context import RefreshReport, RefreshRun
from hockey_model.store.refresh_nhl import score_days
from hockey_model.store.refresh_other import daily_faceoff, espn_injuries, espn_starters, moneypuck

_STEPS = (nhl, moneypuck, espn_starters, espn_injuries, daily_faceoff)
# The sources that change between the morning refresh and puck drop: game times and states,
# probable and confirmed goalies, and injuries. The boxscores and MoneyPuck files do not.
_STARTER_STEPS = (score_days, espn_starters, espn_injuries, daily_faceoff)

__all__ = ["RefreshReport", "refresh", "refresh_starters"]


def _run(run: RefreshRun, steps: tuple[Callable[[RefreshRun], None], ...]) -> RefreshReport:
    if run.now.tzinfo is None:
        raise ValueError(f"refresh: now {run.now!r} has no timezone; pass a UTC datetime")
    for step in steps:
        step(run)
    return run.report()


def refresh(
    conn: sqlite3.Connection,
    client: HttpClient,
    *,
    now: datetime,
    backfill_from: date | None = None,
    clock: Callable[[], datetime] | None = None,
    budget: timedelta | None = None,
    reread_moneypuck: bool = False,
    retry_skipped: bool = False,
) -> RefreshReport:
    """Read every source in order, and report what was written and what failed.

    Each source runs even when an earlier one failed. A failed unit of work writes nothing.
    ``clock`` stamps each read with its own fetch time. Without it, every read gets ``now``.
    ``budget`` is how long the NHL steps may read, counted from ``now``. A backfill that
    spends it stops, and the report says the backfill is not complete. ``retry_skipped`` reads
    the units that earlier reads left as known gaps, once more.
    """
    if budget is not None and clock is None:
        raise ValueError("refresh: a budget needs a clock; pass clock to count the time spent")
    deadline = None if budget is None else now + budget
    run = RefreshRun(
        conn,
        client,
        now,
        backfill_from,
        clock,
        deadline,
        reread_moneypuck=reread_moneypuck,
        retry_skipped=retry_skipped,
    )
    return _run(run, _STEPS)


def refresh_starters(
    conn: sqlite3.Connection,
    client: HttpClient,
    *,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
) -> RefreshReport:
    """Read only the schedule, the goalie sources, and the injuries, as ``refresh`` does."""
    return _run(RefreshRun(conn, client, now, None, clock), _STARTER_STEPS)
