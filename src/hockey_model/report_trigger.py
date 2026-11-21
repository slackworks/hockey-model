"""Decide whether one scheduled trigger publishes the daily report.

The scheduler fires at minute ``TRIGGER_MINUTE_UTC`` of every hour. Every trigger that fires
before the first puck drop of a game day is due. The gate then skips a day that already has a
release, so the first run that starts before the puck drop publishes. A day with no release
also publishes late, because the games that have not started still need a report (see
report_gate.py). A game day with no games has its first puck drop at the end of the day. Game
days are Eastern dates: see game_day.py. How the minute was chosen:
docs/design/daily-report-triggers.md.

``prep_blocks_report`` names a prepare run that holds the store group past the first puck
drop, because GitHub counts a job timeout from its start, so the gate skips a run that would
queue behind it (#163).
"""

from datetime import datetime, timedelta

from hockey_model.types import HockeyModelError
from hockey_model.utc import require_utc

# GitHub delays and drops scheduled runs most at the top of the hour, and many workflows also
# use :15, :30, and :45. :44 avoids those times.
TRIGGER_MINUTE_UTC = 44


def first_puck_drop(day_closes: datetime, starts: list[datetime]) -> datetime:
    """Return the earliest start, or ``day_closes`` when the game day has no games."""
    require_utc(day_closes, "game day close")
    for start in starts:
        require_utc(start, "game start")
    return min(starts) if starts else day_closes


# The prepare job times out at this length (timeout-minutes in daily-prepare.yml). GitHub counts
# a job timeout from the job start, so a prepare that holds the store group ends within it.
PREP_TIMEOUT = timedelta(minutes=180)


def puck_drop_ahead(now: datetime, first_start: datetime, margin: timedelta | None = None) -> bool:
    """Return True while a run that starts at ``now`` can publish before the puck drop.

    Pass the time of the run itself. ``margin`` is the time the run needs after it starts,
    such as the report budget. A run that finishes after the puck drop cannot replace a
    published report, so the gate skips the replace. A day with no release still publishes late.
    """
    require_utc(now, "report trigger now")
    require_utc(first_start, "first puck drop")
    needed = timedelta(0) if margin is None else margin
    if needed < timedelta(0):
        raise HockeyModelError(f"report margin must not be negative, got {needed}")
    return now + needed < first_start


def prep_blocks_report(
    prep_started: datetime, first_start: datetime, margin: timedelta | None = None
) -> bool:
    """Return True when a running prepare can hold the store group past the first puck drop.

    The report and the prepare share one concurrency group, so the report waits for the
    prepare to end. GitHub counts a job timeout from the job start, so a prepare that
    started late runs past its window (#163). A prepare that started on time ends inside
    its window, and the report can wait for it.

    ``margin`` is the time the report needs after it starts, such as the report budget. The
    report starts only when the prepare ends, so the two times add. The boundary is the same
    as in ``puck_drop_ahead``: a report that finishes at the puck drop is not ahead of it.
    """
    require_utc(prep_started, "prepare run start")
    require_utc(first_start, "first puck drop")
    needed = timedelta(0) if margin is None else margin
    if needed < timedelta(0):
        raise HockeyModelError(f"report margin must not be negative, got {needed}")
    return prep_started + PREP_TIMEOUT + needed >= first_start
