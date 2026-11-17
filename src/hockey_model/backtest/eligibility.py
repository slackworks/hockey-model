"""Which seasons a backtest scored, and whether the scored games can support a conclusion."""

import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from hockey_model.backtest.metrics import bootstrap_problem
from hockey_model.game_model.seasons import (
    EXPECTED_REGULAR_GAMES,
    MIN_SEASON_COVERAGE,
    REGULAR_SEASON_TYPE,
    SeasonPlan,
)
from hockey_model.sources.nhl import FINAL_STATES
from hockey_model.store.reads import unresolved_games
from hockey_model.store.refresh_nhl import UNRESOLVED_AFTER
from hockey_model.store.sql_text import placeholders
from hockey_model.store.timestamps import parse_iso_utc


@dataclass(frozen=True, slots=True)
class SeasonCount:
    """The scored games of one season and game type, and what the season should hold.

    ``expected`` is set for a regular season in ``EXPECTED_REGULAR_GAMES``, else ``None``.
    ``unresolved`` counts stored games of the season that are still not final long after
    the newest final game began, so a gap in the schedule shows apart from the count.
    """

    season: int
    game_type: int
    scored: int
    expected: int | None
    unresolved: int


def _expected_seasons(plan: SeasonPlan) -> list[int]:
    """Return the seasons of the plan that have an expected count."""
    if plan.game_types is not None and REGULAR_SEASON_TYPE not in plan.game_types:
        return []
    low, high = plan.scored
    # An open-ended plan has a huge upper bound, so it expects every listed later season.
    return [s for s in sorted(EXPECTED_REGULAR_GAMES) if low <= s <= high]


def _unresolved(conn: sqlite3.Connection) -> Counter[tuple[int, int]]:
    """Count the stored games, by season and game type, not final long after the newest final."""
    marks = placeholders(FINAL_STATES)
    (newest,) = conn.execute(
        f"SELECT MAX(start_utc) FROM latest_games WHERE state IN ({marks})", sorted(FINAL_STATES)
    ).fetchone()
    if newest is None:
        return Counter()
    found = unresolved_games(conn, parse_iso_utc(newest) - UNRESOLVED_AFTER)
    return Counter((season, game_type) for _, season, game_type, _ in found)


def season_counts(
    conn: sqlite3.Connection, scored: Iterable[tuple[int, int]], plan: SeasonPlan
) -> tuple[SeasonCount, ...]:
    """Return the scored count of each season and game type, in season then type order.

    ``scored`` has one ``(season, game_type)`` pair for each scored game. A season of the plan
    that has an expected count is listed even with no scored game.
    """
    counts = Counter(scored)
    unresolved = _unresolved(conn)
    keys = set(counts) | {(season, REGULAR_SEASON_TYPE) for season in _expected_seasons(plan)}
    keys |= {key for key in unresolved if plan.scored[0] <= key[0] <= plan.scored[1]}
    return tuple(
        SeasonCount(
            season,
            game_type,
            counts[(season, game_type)],
            EXPECTED_REGULAR_GAMES.get(season) if game_type == REGULAR_SEASON_TYPE else None,
            unresolved[(season, game_type)],
        )
        for season, game_type in sorted(keys)
    )


def _coverage_problem(count: SeasonCount) -> str | None:
    if count.expected is None or count.game_type != REGULAR_SEASON_TYPE:
        return None
    if count.scored == 0:
        return (
            f"season {count.season} has no scored regular season games; expected {count.expected}"
        )
    if count.scored < count.expected * MIN_SEASON_COVERAGE:
        return (
            f"season {count.season} has {count.scored} scored regular season games; "
            f"expected {count.expected}, and at least {MIN_SEASON_COVERAGE:.0%} is required"
        )
    return None


def ineligibility_reasons(counts: Iterable[SeasonCount], scored_games: int) -> tuple[str, ...]:
    """Return why the scored games cannot support a success claim, or an empty tuple.

    A season with an expected count must be covered. The scored games must also hold enough
    independent blocks for the bootstrap. The scores stay descriptive when this is not met.
    """
    reasons = [p for count in counts if (p := _coverage_problem(count)) is not None]
    problem = bootstrap_problem(scored_games)
    if problem is not None:
        reasons.append(problem)
    return tuple(reasons)
