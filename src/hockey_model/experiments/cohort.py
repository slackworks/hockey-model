"""The cohort a confirmation run scores: its plan, its cutoff instant, and its size checks.

Design: docs/design/promotion-bar.md
"""

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, time

from hockey_model.experiments.looks import promotion_tail
from hockey_model.experiments.models import ExperimentRecord
from hockey_model.game_model.seasons import SeasonPlan, confirmation_plan, in_cohort, season_of
from hockey_model.store.reads import LATEST_GAME, final_games_before
from hockey_model.types import HockeyModelError


def cutoff_instant(record: ExperimentRecord) -> datetime:
    """Return the instant after which games are in the cohort.

    ``data_cutoff`` means "data through this day": the candidate may have used any game that
    started on that day. The instant is the last microsecond of that day in UTC, and the cohort
    holds the games that start after it. Game starts are stored in UTC.
    """
    try:
        return datetime.combine(date.fromisoformat(record.data_cutoff), time.max, tzinfo=UTC)
    except ValueError as exc:
        raise HockeyModelError(
            f"experiment {record.id!r}: data_cutoff {record.data_cutoff!r} is not a YYYY-MM-DD date"
        ) from exc


def plan_for(record: ExperimentRecord) -> SeasonPlan:
    """Return the plan of a confirmation record, with the interval tail of its look."""
    plan = confirmation_plan(cutoff_instant(record))
    return replace(plan, interval_tail=promotion_tail(record.look_number))


def floor_notes(record: ExperimentRecord, plan: SeasonPlan) -> list[str]:
    """Say so when the confirmation floor moved the first scored season."""
    cutoff = plan.cutoff
    natural = season_of(cutoff.date()) if cutoff is not None else plan.scored[0]
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
    """Refuse a run that scored fewer games than the cohort held before the run.

    A game dropped during the run, for example for a missing feature, would otherwise record
    a smaller cohort than the one the record registered a minimum for.
    """
    if scored != cohort or scored < record.min_games:
        raise HockeyModelError(
            f"experiment {record.id!r}: the run scored {scored} games, but the cohort holds "
            f"{cohort} games and the registered minimum is {record.min_games}; the record is "
            "unchanged. Find why the run dropped games, then run again"
        )
