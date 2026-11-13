"""Decide whether one scheduled trigger publishes the daily report.

The scheduler fires at minute ``TRIGGER_MINUTE_UTC`` of each hour in ``TRIGGER_HOURS_UTC``.
Every trigger that fires before the first puck drop of a game day is due. The gate then
skips a day that already has a release, so the first run that starts before the puck drop
publishes. A day with no release also publishes late, because the games that have not started
still need a report (see report_gate.py). A game day with
no games has its first puck drop at the end of the day. Game days are Eastern dates: see
game_day.py. How the hours and the minute were chosen: docs/design/daily-report-triggers.md.
"""

from datetime import datetime, timedelta

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


# The morning prep job times out at 180 minutes (daily-prepare.yml). Until a prep can have run
# its full window, a missing prep marker means "not yet", not "broken".
PREP_WINDOW = timedelta(minutes=180)


def prep_too_early(now: datetime, first_start: datetime) -> bool:
    """Return True when the first puck drop is more than ``PREP_WINDOW`` away.

    A run this early that finds no prep marker is not an error. Inside the window the same
    missing marker means the prep failed, and a person has to act.
    """
    require_utc(now, "report trigger now")
    require_utc(first_start, "first puck drop")
    return first_start - now > PREP_WINDOW


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
