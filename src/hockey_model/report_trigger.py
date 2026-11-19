"""Decide whether one scheduled trigger publishes the daily report.

The scheduler fires at minute ``TRIGGER_MINUTE_UTC`` of each hour in ``TRIGGER_HOURS_UTC``.
Every trigger that fires before the first puck drop of a game day is due. The gate then
skips a day that already has a release, so the first run that starts before the puck drop
publishes. A day with no release also publishes late, because the games that have not started
still need a report (see report_gate.py). A game day with
no games has its first puck drop at the end of the day. Game days are Eastern dates: see
game_day.py. How the hours and the minute were chosen: docs/design/daily-report-triggers.md.

Two facts also stop a run. ``prep_too_early`` skips a missing prepared marker while the
morning prep window is still open. ``prep_blocks_report`` names a prepare run that holds the
store group past the first puck drop, because GitHub counts a job timeout from its start, so
the gate skips a run that would queue behind it (#163).
"""

from datetime import UTC, date, datetime, timedelta

from hockey_model.types import HockeyModelError
from hockey_model.utc import require_utc

# GitHub delays and drops scheduled runs most at the top of the hour, and many workflows also
# use :15, :30, and :45. :44 avoids those times.
TRIGGER_MINUTE_UTC = 44
# The UTC hours that hold a game day's trigger for the 2026-27 schedule. The workflow cron
# MUST list the same minute and hours. Re-derive them each season with
# scripts/trigger_hours.py.
TRIGGER_HOURS_UTC = (0, 11, 15, 16, 17, 18, 19, 20, 21, 22, 23)


def first_puck_drop(day_closes: datetime, starts: list[datetime]) -> datetime:
    """Return the earliest start, or ``day_closes`` when the game day has no games."""
    require_utc(day_closes, "game day close")
    for start in starts:
        require_utc(start, "game start")
    return min(starts) if starts else day_closes


# The morning prep job times out at 180 minutes (daily-prepare.yml). The window opens at its
# cron, so a prep that starts on time still fits inside it.
PREP_WINDOW = timedelta(minutes=180)
# The hour of the daily-prepare cron, and the longest queue delay that the workflow accepts.
# GitHub delays a scheduled run, and the delay is not part of the job timeout. The report run
# skips a missing prep until this late send can have finished; after that it fails (#163).
PREP_CRON_HOUR_UTC = 8
PREP_QUEUE_ALLOWANCE = timedelta(minutes=60)


def prep_window_opened(day: date) -> datetime:
    """Return the instant that the prep job of game day ``day`` is due to start."""
    return datetime(day.year, day.month, day.day, PREP_CRON_HOUR_UTC, tzinfo=UTC)


def prep_too_early(
    now: datetime, first_start: datetime, day: date, queue: timedelta = PREP_QUEUE_ALLOWANCE
) -> bool:
    """Return True while the prep job of ``day`` still has time to run.

    A run this early that finds no prep marker is not an error. After the window, the same
    missing marker means the prep failed, and a person has to act. ``queue`` is the longest
    time from the prep cron to its job start that the caller treats as a late send.
    """
    require_utc(now, "report trigger now")
    require_utc(first_start, "first puck drop")
    if queue < timedelta(0):
        raise HockeyModelError(f"prep queue allowance must not be negative, got {queue}")
    by_window = now <= prep_window_opened(day) + PREP_WINDOW + queue
    return by_window and first_start - now > PREP_WINDOW


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
    return prep_started + PREP_WINDOW + needed >= first_start
