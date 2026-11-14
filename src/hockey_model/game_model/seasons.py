"""The seasons that the fit and the backtest use. A season is written as the NHL writes it."""

from dataclasses import dataclass
from datetime import date, datetime

from hockey_model.game_day import game_day_of
from hockey_model.sources.nhl import GameRow

# The Elo replays from this season. Its games set the ratings and are never in a fit.
ELO_BURN_IN = 20142015
# The first season in a fit.
FIT_FIRST = 20152016
# Features are chosen on these seasons only. The fit uses the first range, and the
# second range is the validation that can veto a feature.
SELECT_TRAIN = (20152016, 20212022)
SELECT_VALIDATE = (20222023, 20232024)
# The seasons that are scored, once each.
SCORED = (20242025, 20252026)

# Experiment cohorts.
# EXPLORATION covers every season through 2025-26 that a walk-forward fit can score.
# A scored season needs a fit from an earlier season, so the cohort starts one season
# after FIT_FIRST. Results from these seasons are labeled exploratory and never
# promote a feature. SCORED seasons are inside the range: the backtest report already
# scored them, so an exploration run on them is a repeat look, not a fresh one.
EXPLORATION = (20162017, 20252026)
# CONFIRMATION starts with the 2026-27 season. The owner approved the regular season
# only, so playoff games (NHL game type 3) are outside the cohort.
CONFIRMATION_START = 20262027
# The NHL reports a regular season game as game type 2.
REGULAR_SEASON_TYPE = 2
# A season later than any real season, so a confirmation plan scores every season
# the store holds from its first confirmation season onward.
OPEN_END = 99_999_999


# Regular season games that the NHL played, by season. The count differs with the number of
# teams, a shortened schedule, and a pause, so it is policy data and not a formula. A season
# that is not listed has no expected count and is not checked for coverage.
EXPECTED_REGULAR_GAMES = {
    20152016: 1230,
    20162017: 1230,
    20172018: 1271,
    20182019: 1271,
    20192020: 1082,  # the season stopped in March 2020
    20202021: 868,  # a 56-game season
    20212022: 1312,
    20222023: 1312,
    20232024: 1312,
    20242025: 1312,
    20252026: 1312,
}
# A scored regular season counts as complete from this share of its expected games. A hole
# from a failed score day, or a season that is still loading, falls under it.
MIN_SEASON_COVERAGE = 0.95

# A season is counted from this month of its first year until the same month a year later.
# The cohorts, the season filter, and the MoneyPuck refresh count the season from the Eastern
# game day. The fit default is the exception, as its open issue records.
SEASON_START_MONTH = 9


def season_start_year(day: date) -> int:
    """Return the first calendar year of the season that a date falls in."""
    return day.year if day.month >= SEASON_START_MONTH else day.year - 1


def season_code(start_year: int) -> int:
    """Return the season id, such as 20262027, of the season that starts in ``start_year``."""
    return start_year * 10000 + start_year + 1


def season_of(day: date) -> int:
    """Return the NHL season id that a date falls in."""
    return season_code(season_start_year(day))


# Each end of the bootstrap interval of a first look cuts this share of the resampled means.
# A later look cuts less (experiments/looks.py).
INTERVAL_TAIL = 0.05


@dataclass(frozen=True, slots=True)
class SeasonPlan:
    """The season ranges of one backtest. A test passes a short plan."""

    burn_in: int = ELO_BURN_IN
    fit_first: int = FIT_FIRST
    select_train: tuple[int, int] = SELECT_TRAIN
    select_validate: tuple[int, int] = SELECT_VALIDATE
    scored: tuple[int, int] = SCORED
    # Game types the plan scores. None keeps every game type the store holds.
    game_types: tuple[int, ...] | None = None
    # Score only games that start after this instant. A confirmation record uses its
    # registration cutoff, so a spent cohort is never scored twice.
    cutoff: datetime | None = None
    # The share of the bootstrap each end of the interval cuts. A later look cuts less.
    interval_tail: float = INTERVAL_TAIL


def confirmation_plan(cutoff: datetime) -> SeasonPlan:
    """Return the plan of a confirmation run.

    The cohort is the regular season of the season the cutoff falls in, and every
    later season. Only games that start after the cutoff are scored.
    """
    # The floor keeps a record with an early cutoff from scoring exploration seasons.
    first_season = max(CONFIRMATION_START, season_of(game_day_of(cutoff)))
    return SeasonPlan(
        scored=(first_season, OPEN_END),
        game_types=(REGULAR_SEASON_TYPE,),
        cutoff=cutoff,
    )


def exploration_plan() -> SeasonPlan:
    """Return the plan of an exploration run: every season through 2025-26."""
    return SeasonPlan(scored=EXPLORATION)


def in_cohort(game: GameRow, plan: SeasonPlan) -> bool:
    """Return True when the plan scores this game."""
    if not plan.scored[0] <= game.season <= plan.scored[1]:
        return False
    if plan.game_types is not None and game.game_type not in plan.game_types:
        return False
    return plan.cutoff is None or game.start > plan.cutoff


def scored_label(plan: SeasonPlan) -> str:
    """Describe the seasons a plan scores, in words a reader can check."""
    if plan.scored[1] == OPEN_END:
        return f"seasons from {plan.scored[0]} onward"
    return f"seasons {plan.scored[0]} to {plan.scored[1]}"


DEFAULT_PLAN = SeasonPlan()
