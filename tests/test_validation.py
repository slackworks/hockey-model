"""Tests for the projection validation that every public entry point shares."""

import math
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model import (
    Game,
    GamePrediction,
    HockeyModelError,
    Player,
    ProbabilityError,
    Projection,
    Roster,
    Slot,
    Standing,
    game_win_probability,
    matchup,
    playoff_probability,
    project_player,
    project_team,
)
from hockey_model.models import FixedStatModel
from hockey_model.types import ProjectionError
from hockey_model.validation import (
    LEVEL_TOLERANCE,
    Domain,
    stat_domain,
    validate_probability,
    validate_projection,
)

LEVEL = 0.8
GOOD = Projection(0.4, 0.1, 0.7, LEVEL)


def _player(canonical_id: str = "a") -> Player:
    return Player(canonical_id, None, None, canonical_id, frozenset({Slot.C}), "TOR")


class _Raw:
    """A projection-shaped object that skips Projection's own checks."""

    def __init__(self, point, low, high, level) -> None:
        self.point, self.low, self.high, self.level = point, low, high, level


class TestValidateProjection:
    def test_a_good_projection_passes(self) -> None:
        assert validate_projection(GOOD, LEVEL, "op", Domain.PROBABILITY) is GOOD

    @pytest.mark.parametrize("value", [None, 0.4, (0.4, 0.1, 0.7, LEVEL), {"point": 1}])
    def test_a_value_that_is_not_a_projection_fails(self, value: object) -> None:
        with pytest.raises(ProjectionError, match="op: the model returned .*expected a Projection"):
            validate_projection(value, LEVEL, "op", None)

    def test_a_wrong_level_fails(self) -> None:
        with pytest.raises(ProjectionError, match="level 0.9 but 0.8 was requested"):
            validate_projection(Projection(0.4, 0.1, 0.7, 0.9), LEVEL, "op", None)

    def test_a_level_within_the_tolerance_passes(self) -> None:
        near = Projection(0.4, 0.1, 0.7, LEVEL + LEVEL_TOLERANCE / 2)
        assert validate_projection(near, LEVEL, "op", None) is near

    def test_a_point_outside_the_interval_fails(self) -> None:
        with pytest.raises(ProjectionError, match="point 0.9 is outside"):
            validate_projection(Projection(0.9, 0.1, 0.7, LEVEL), LEVEL, "op", None)

    @pytest.mark.parametrize(
        "raw",
        [
            _Raw(True, 0.0, 1.0, LEVEL),
            _Raw(0.5, False, 1.0, LEVEL),
            _Raw(0.5, 0.0, 1.0, True),
            _Raw("0.5", 0.0, 1.0, LEVEL),
            _Raw(float("nan"), 0.0, 1.0, LEVEL),
            _Raw(0.5, float("-inf"), 1.0, LEVEL),
        ],
    )
    def test_a_malformed_field_fails(self, raw: _Raw) -> None:
        # A plugin can return an object that skips Projection's own checks.
        with pytest.raises(ProjectionError):
            validate_projection(raw, LEVEL, "op", None)

    def test_a_negative_count_fails(self) -> None:
        with pytest.raises(ProjectionError, match="below zero"):
            validate_projection(Projection(1.0, -0.5, 2.0, LEVEL), LEVEL, "op", Domain.NONNEGATIVE)

    def test_a_probability_outside_zero_to_one_raises_a_probability_error(self) -> None:
        with pytest.raises(ProbabilityError, match=r"in \[0, 1\]"):
            validate_projection(Projection(0.5, 0.1, 1.2, LEVEL), LEVEL, "op", Domain.PROBABILITY)

    def test_the_endpoints_of_the_probability_domain_pass(self) -> None:
        edge = Projection(0.0, 0.0, 1.0, LEVEL)
        assert validate_projection(edge, LEVEL, "op", Domain.PROBABILITY) is edge

    def test_a_goals_against_average_above_one_passes(self) -> None:
        high = Projection(2.8, 2.1, 3.4, LEVEL)
        assert validate_projection(high, LEVEL, "op", stat_domain("gaa")) is high

    def test_a_negative_value_passes_the_structural_domain(self) -> None:
        # An extension stat such as a plus-minus can be negative.
        neg = Projection(-1.0, -3.0, 1.0, LEVEL)
        assert validate_projection(neg, LEVEL, "op", None) is neg


class TestStatDomain:
    @pytest.mark.parametrize("stat", ["save_pct", "shooting_pct"])
    def test_fractional_rates_are_probabilities(self, stat: str) -> None:
        assert stat_domain(stat) is Domain.PROBABILITY

    @pytest.mark.parametrize("stat", ["goals", "assists", "gaa", "goals_against_average"])
    def test_counts_and_goals_against_average_are_nonnegative(self, stat: str) -> None:
        assert stat_domain(stat) is Domain.NONNEGATIVE

    def test_an_unknown_stat_gets_only_the_structural_checks(self) -> None:
        assert stat_domain("plus_minus") is None


class TestValidateProbability:
    @pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
    def test_a_probability_passes(self, value: float) -> None:
        validate_probability(value, "op", "home win")

    @pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True, "0.5", None])
    def test_anything_else_fails(self, value: object) -> None:
        with pytest.raises(ProbabilityError, match="op: invalid home win"):
            validate_probability(value, "op", "home win")


class _Stat:
    def __init__(self, result: object) -> None:
        self.result = result

    def project(self, player, stat, horizon_games, level):
        return self.result


class _Joint:
    def __init__(self, result: object) -> None:
        self.result = result

    def project_total(self, players, stat, horizon_games, level):
        return self.result


_UNSET = object()


class _Match:
    def __init__(self, wins: object = (0.6, 0.4), goals: object = _UNSET) -> None:
        self.wins = wins
        self.goals = Projection(3.0, 1.0, 5.0, LEVEL) if goals is _UNSET else goals

    def win_probabilities(self, home, away):
        return self.wins

    def expected_goals(self, team, level):
        return self.goals


class _Playoffs:
    def __init__(self, result: object) -> None:
        self.result = result

    def playoff_odds(self, team_code, standings, level):
        return self.result


class _Game:
    def __init__(self, result: object) -> None:
        self.result = result

    def predict(self, game, as_of, level):
        return self.result


BAD_PROJECTIONS = [
    pytest.param(None, id="none"),
    pytest.param(Projection(0.4, 0.1, 0.7, 0.5), id="wrong-level"),
    pytest.param(_Raw(0.9, 0.1, 0.7, LEVEL), id="point-outside"),
    pytest.param(_Raw(float("nan"), 0.1, 0.7, LEVEL), id="not-finite"),
    pytest.param(Projection(-1.0, -2.0, -0.5, LEVEL), id="negative-count"),
]


class TestPublicEntryPoints:
    @pytest.mark.parametrize("bad", BAD_PROJECTIONS)
    def test_project_player_rejects_a_bad_result(self, bad: object) -> None:
        with pytest.raises(ProjectionError):
            project_player(_player(), "goals", _Stat(bad), level=LEVEL)

    def test_project_player_accepts_a_good_result(self) -> None:
        model = FixedStatModel({("a", "goals"): GOOD})
        assert project_player(_player(), "goals", model, level=LEVEL) == GOOD

    def test_project_player_bounds_a_fractional_rate(self) -> None:
        with pytest.raises(ProbabilityError):
            project_player(_player(), "save_pct", _Stat(Projection(0.9, 0.8, 1.3, LEVEL)))

    def test_project_player_accepts_a_goals_against_average_above_one(self) -> None:
        high = Projection(2.8, 2.1, 3.4, LEVEL)
        assert project_player(_player(), "gaa", _Stat(high)) == high

    @pytest.mark.parametrize("bad", BAD_PROJECTIONS)
    def test_project_team_rejects_a_bad_player_result(self, bad: object) -> None:
        roster = Roster("t", (_player("a"),), None)
        with pytest.raises(ProjectionError):
            project_team(roster, "goals", _Stat(bad), _Joint(GOOD), level=LEVEL)

    @pytest.mark.parametrize("bad", BAD_PROJECTIONS)
    def test_project_team_rejects_a_bad_joint_total(self, bad: object) -> None:
        roster = Roster("t", (_player("a"),), None)
        with pytest.raises(ProjectionError):
            project_team(roster, "goals", _Stat(GOOD), _Joint(bad), level=LEVEL)

    def test_project_team_skips_the_bench(self) -> None:
        bench = _player("b")
        roster = Roster("t", (_player("a"), bench), {"a": Slot.C, "b": Slot.BENCH})
        seen: list[str] = []

        class Spy(_Stat):
            def project(self, player, stat, horizon_games, level):
                seen.append(player.canonical_id)
                return GOOD

        project_team(roster, "goals", Spy(GOOD), _Joint(GOOD), level=LEVEL)
        assert seen == ["a"]

    @pytest.mark.parametrize("bad", BAD_PROJECTIONS)
    def test_matchup_rejects_a_bad_goal_projection(self, bad: object) -> None:
        roster = Roster("t", (_player("a"),), None)
        with pytest.raises(ProjectionError):
            matchup(roster, roster, _Match(goals=bad), LEVEL)

    @pytest.mark.parametrize("wins", [None, (0.6,), (True, 0.4), (0.6, "0.4")])
    def test_matchup_rejects_a_bad_win_pair(self, wins: object) -> None:
        roster = Roster("t", (_player("a"),), None)
        with pytest.raises(ProbabilityError):
            matchup(roster, roster, _Match(wins=wins), LEVEL)

    @pytest.mark.parametrize("bad", BAD_PROJECTIONS)
    def test_playoff_probability_rejects_a_bad_result(self, bad: object) -> None:
        standings = (Standing("TOR", 10, 12, 5, 8),)
        with pytest.raises((ProjectionError, ProbabilityError)):
            playoff_probability("TOR", standings, _Playoffs(bad), LEVEL)

    def test_playoff_probability_still_bounds_the_odds(self) -> None:
        standings = (Standing("TOR", 10, 12, 5, 8),)
        with pytest.raises(ProbabilityError):
            playoff_probability("TOR", standings, _Playoffs(Projection(0.7, 0.5, 1.4, LEVEL)))

    @pytest.mark.parametrize("bad", BAD_PROJECTIONS)
    def test_game_win_probability_rejects_a_bad_goal_projection(self, bad: object) -> None:
        game = Game(1, "TOR", "BOS", datetime(2026, 1, 20, tzinfo=UTC))
        result = GamePrediction(0.6, 0.4, bad, GOOD, (), ())  # ty: ignore[invalid-argument-type]
        as_of = datetime(2026, 1, 19, tzinfo=UTC)
        with pytest.raises(ProjectionError):
            game_win_probability(game, _Game(result), as_of=as_of, level=LEVEL)

    @pytest.mark.parametrize("result", [None, (0.6, 0.4), {"home_win": 0.6}])
    def test_game_win_probability_rejects_a_result_of_the_wrong_type(self, result: object) -> None:
        game = Game(1, "TOR", "BOS", datetime(2026, 1, 20, tzinfo=UTC))
        as_of = datetime(2026, 1, 19, tzinfo=UTC)
        with pytest.raises(ProjectionError, match="expected a GamePrediction"):
            game_win_probability(game, _Game(result), as_of=as_of, level=LEVEL)

    def test_the_error_is_a_hockey_model_error(self) -> None:
        assert issubclass(ProjectionError, HockeyModelError)


_FINITE = st.floats(min_value=-5.0, max_value=5.0)
_ANY_FLOAT = st.floats(allow_nan=True, allow_infinity=True)


@settings(max_examples=100, deadline=None)
@given(_FINITE, _FINITE, _FINITE, st.sampled_from([Domain.PROBABILITY, Domain.NONNEGATIVE]))
def test_an_invalid_projection_never_comes_back_as_valid(
    point: float, low: float, high: float, domain: Domain
) -> None:
    low, high = sorted((low, high))
    value = Projection(point, low, high, LEVEL)
    bounds = (point, low, high)
    lowest = 0.0
    highest = 1.0 if domain is Domain.PROBABILITY else math.inf
    valid = (
        all(math.isfinite(v) and lowest <= v <= highest for v in bounds) and low <= point <= high
    )
    if valid:
        assert validate_projection(value, LEVEL, "op", domain) is value
    else:
        with pytest.raises((ProjectionError, ProbabilityError)):
            validate_projection(value, LEVEL, "op", domain)


@settings(max_examples=60, deadline=None)
@given(_ANY_FLOAT)
def test_a_probability_is_valid_exactly_when_it_is_finite_and_in_range(value: float) -> None:
    if math.isfinite(value) and 0.0 <= value <= 1.0:
        validate_probability(value, "op", "p")
    else:
        with pytest.raises(ProbabilityError):
            validate_probability(value, "op", "p")


@given(
    low=st.floats(0, 100, allow_nan=False),
    width=st.floats(0, 100, allow_nan=False),
    frac=st.floats(0, 1, allow_nan=False),
)
def test_a_valid_nonnegative_projection_always_passes(
    low: float, width: float, frac: float
) -> None:
    high = low + width
    point = min(max(low + frac * width, low), high)
    projection = Projection(point, low, high, LEVEL)
    assert validate_projection(projection, LEVEL, "op", Domain.NONNEGATIVE) is projection
