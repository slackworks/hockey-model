"""The cohort a confirmation run scores: its plan, its cutoff instant, and its size checks.

Design: docs/design/promotion-bar.md
"""

import re
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta

from hockey_model.experiments.looks import promotion_tail
from hockey_model.experiments.models import ExperimentRecord
from hockey_model.game_day import game_day_of, game_day_window
from hockey_model.game_model.seasons import SeasonPlan, confirmation_plan, in_cohort, season_of
from hockey_model.store.reads import LATEST_GAME, final_games_before
from hockey_model.types import HockeyModelError

# fromisoformat also reads 20261031 and 2026-W44-6, which are not the registered form.
_CUTOFF_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def cutoff_instant(record: ExperimentRecord) -> datetime:
    """Return the instant after which games are in the cohort.

    ``data_cutoff`` means "data through this day": the candidate may have used any game of that
    Eastern game day (see game_day.py). The instant is the last microsecond of that game day,
    and the cohort holds the games that start after it. Game starts are stored in UTC.
    """
    problem = (
        f"experiment {record.id!r}: data_cutoff {record.data_cutoff!r} is not a YYYY-MM-DD date"
    )
    if not _CUTOFF_SHAPE.fullmatch(record.data_cutoff):
        raise HockeyModelError(problem)
    try:
        closes = game_day_window(date.fromisoformat(record.data_cutoff))[1]
        return closes - timedelta(microseconds=1)
    except ValueError as exc:
        raise HockeyModelError(problem) from exc


def plan_for(record: ExperimentRecord) -> SeasonPlan:
    """Return the plan of a confirmation record, with the interval tail of its look."""
    plan = confirmation_plan(cutoff_instant(record))
    return replace(plan, interval_tail=promotion_tail(record.look_number))


def floor_notes(record: ExperimentRecord, plan: SeasonPlan) -> list[str]:
    """Say so when the confirmation floor moved the first scored season."""
    cutoff = plan.cutoff
    natural = season_of(game_day_of(cutoff)) if cutoff is not None else plan.scored[0]
    if natural == plan.scored[0]:
        return []
    return [
        f"data_cutoff {record.data_cutoff} falls in season {natural}, before the confirmation "
        f"start; the cohort begins at season {plan.scored[0]}."
    ]


def require_cohort_size(
    conn: sqlite3.Connection, record: ExperimentRecord, plan: SeasonPlan
) -> int:
    """Return the cohort size, and refuse a confirmation run before it reaches ``min_games``."""
    games = [g for g in final_games_before(conn, LATEST_GAME) if in_cohort(g, plan)]
    if len(games) < record.min_games:
        raise HockeyModelError(
            f"experiment {record.id!r}: the confirmation cohort has {len(games)} games, "
            f"below the registered minimum of {record.min_games}; run "
            "`hockey-model refresh` and try again after more cohort games finish"
        )
    return len(games)


def require_scored_cohort(record: ExperimentRecord, scored: int, cohort: int) -> None:
    """Refuse a run that scored a different count of games than the cohort held before it.

    A game dropped during the run, for example for a missing feature, would otherwise record
    a smaller cohort than the one the pre-run check counted against ``min_games``.
    """
    if scored != cohort:
        raise HockeyModelError(
            f"experiment {record.id!r}: the run scored {scored} games, but the cohort held "
            f"{cohort} games before the run; the record is unchanged. "
            "Find why the run dropped games, then run again"
        )
