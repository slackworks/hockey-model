"""Roster matchup and playoff probability entry points."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from hockey_model.models import MatchupModel, StandingsModel
from hockey_model.roster import validate_roster
from hockey_model.types import ProbabilityError, Projection, Roster, Standing
from hockey_model.validation import Domain, validate_probability, validate_projection


@dataclass(frozen=True, slots=True)
class MatchupResult:
    """Win probabilities and expected-goal intervals for two rosters."""

    home_win: float
    away_win: float
    home_goals: Projection
    away_goals: Projection


def matchup(home: Roster, away: Roster, model: MatchupModel, level: float = 0.8) -> MatchupResult:
    """Project a matchup between any two valid NHL or fantasy rosters."""
    if not 0.0 < level < 1.0:
        raise ValueError("level must be inside the open interval (0, 1)")
    validate_roster(home)
    validate_roster(away)
    pair = model.win_probabilities(home, away)
    check_win_pair(pair, "predict matchup")
    home_win, away_win = pair
    return MatchupResult(
        home_win,
        away_win,
        validate_projection(
            model.expected_goals(home, level),
            level,
            f"predict matchup: expected goals of {home.name!r}",
            Domain.NONNEGATIVE,
        ),
        validate_projection(
            model.expected_goals(away, level),
            level,
            f"predict matchup: expected goals of {away.name!r}",
            Domain.NONNEGATIVE,
        ),
    )


def check_win_pair(values: object, operation: str) -> None:
    """Raise ``ProbabilityError`` unless the pair is two finite values summing to 1.0."""
    if not isinstance(values, (tuple, list)) or len(values) != 2:  # noqa: PLR2004
        raise ProbabilityError(
            f"{operation}: the model returned {values!r}, expected a pair of win probabilities"
        )
    home_win, away_win = values
    validate_probability(home_win, operation, "home win probability")
    validate_probability(away_win, operation, "away win probability")
    if not math.isclose(home_win + away_win, 1.0, abs_tol=1e-9):
        raise ProbabilityError(
            f"{operation}: win probabilities {tuple(values)!r} do not sum to 1.0; "
            "include overtime and shootout wins in one side"
        )


def check_win_interval(point: float, interval: object, operation: str) -> None:
    """Raise ``ProbabilityError`` unless ``interval`` is a ``(low, high)`` pair around ``point``."""
    if not isinstance(interval, (tuple, list)) or len(interval) != 2:  # noqa: PLR2004
        raise ProbabilityError(
            f"{operation}: the win interval is {interval!r}, expected a (low, high) pair"
        )
    low, high = interval
    validate_probability(low, operation, "win interval low")
    validate_probability(high, operation, "win interval high")
    if not low - 1e-9 <= point <= high + 1e-9:
        raise ProbabilityError(
            f"{operation}: the win interval {tuple(interval)!r} does not hold the home win "
            f"chance {point}; return a range around the point"
        )


def playoff_probability(
    team_code: str,
    standings: Sequence[Standing],
    model: StandingsModel,
    level: float = 0.8,
) -> Projection:
    """Project rest-of-season playoff odds for a team present in ``standings``."""
    if not 0.0 < level < 1.0:
        raise ValueError("level must be inside the open interval (0, 1)")
    if all(standing.team_code != team_code for standing in standings):
        raise ValueError(
            f"project playoff probability for {team_code!r}: team is absent from standings; "
            "supply its current standing"
        )
    projection = model.playoff_odds(team_code, standings, level)
    return validate_projection(
        projection, level, f"project playoff probability for {team_code!r}", Domain.PROBABILITY
    )
