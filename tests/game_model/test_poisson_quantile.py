"""Tests for the goal interval that the NHL model builds from a Poisson rate (#71).

An empty store gives both teams the league rate, so each test sets the rate through
``LEAGUE_GOALS`` and reads the interval from ``NhlGameModel.predict``.
"""

import math
import sqlite3
from collections.abc import Iterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from seeding import artifact, utc

from hockey_model.game import Game
from hockey_model.game_model import nhl_model
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.logistic import ELO_ONLY
from hockey_model.game_model.nhl_model import MAX_QUANTILE_STEPS, MAX_RATE, NhlGameModel
from hockey_model.store.db import connect
from hockey_model.types import HockeyModelError, Projection

GAME = Game(100, "TOR", "BOS", utc("2026-01-20T00:00"))
AS_OF = utc("2026-01-19T16:00")


@pytest.fixture(scope="module")
def empty_model(tmp_path_factory: pytest.TempPathFactory) -> Iterator[NhlGameModel]:
    """A model over an empty store. A prediction only reads, so tests share it."""
    conn: sqlite3.Connection = connect(tmp_path_factory.mktemp("quantile") / "store.db")
    yield NhlGameModel(conn, artifact(ELO_ONLY), GoalieMode.GUESS)
    conn.close()


def interval(model: NhlGameModel, rate: float, level: float) -> Projection:
    """Return the home goal interval of a game where both teams have ``rate`` goals."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(nhl_model, "LEAGUE_GOALS", rate)
        return model.predict(GAME, AS_OF, level).home_goals


def log_cdf_quantile(rate: float, probability: float) -> int:
    """Find the smallest count whose Poisson CDF reaches ``probability``, in log space."""
    total = 0.0
    for count in range(100_000):
        total += math.exp(count * math.log(rate) - rate - math.lgamma(count + 1))
        if total >= probability:
            return count
    raise AssertionError(f"no quantile for {rate} at {probability}")


@pytest.mark.parametrize("rate", [0.1, 0.5, 1.0, 3.0, 6.5, 20.0, 60.0, MAX_RATE])
@pytest.mark.parametrize("level", [0.5, 0.8, 0.95, 0.999])
def test_the_interval_matches_an_independent_log_space_cdf(
    empty_model: NhlGameModel, rate: float, level: float
) -> None:
    result = interval(empty_model, rate, level)
    low = log_cdf_quantile(rate, (1.0 - level) / 2.0)
    high = log_cdf_quantile(rate, (1.0 + level) / 2.0)
    assert result.low == min(float(low), rate)
    assert result.high == max(float(high), rate)
    assert result.point == rate
    assert result.level == level


@settings(max_examples=60, deadline=None)
@given(
    rate=st.floats(min_value=0.1, max_value=MAX_RATE),
    level=st.floats(min_value=0.01, max_value=0.99),
)
def test_the_interval_holds_the_point_and_stays_ordered(
    empty_model: NhlGameModel, rate: float, level: float
) -> None:
    result = interval(empty_model, rate, level)
    assert 0.0 <= result.low <= result.point <= result.high


@settings(max_examples=40, deadline=None)
@given(
    rate=st.floats(min_value=0.1, max_value=MAX_RATE),
    levels=st.tuples(
        st.floats(min_value=0.01, max_value=0.98), st.floats(min_value=0.01, max_value=0.98)
    ),
)
def test_a_higher_level_gives_a_wider_interval(
    empty_model: NhlGameModel, rate: float, levels: tuple[float, float]
) -> None:
    narrow, wide = sorted(levels)
    inner = interval(empty_model, rate, narrow)
    outer = interval(empty_model, rate, wide)
    assert outer.low <= inner.low
    assert outer.high >= inner.high


@pytest.mark.parametrize("rate", [math.nan, math.inf, -math.inf, -0.5, MAX_RATE + 0.01, 1e300])
def test_a_rate_outside_the_supported_range_raises(empty_model: NhlGameModel, rate: float) -> None:
    with pytest.raises(HockeyModelError, match="expected goals"):
        interval(empty_model, rate, 0.8)


@pytest.mark.parametrize("level", [0.0, 1.0, -0.1, 1.5, math.nan, math.inf])
def test_a_level_outside_the_open_unit_interval_raises(
    empty_model: NhlGameModel, level: float
) -> None:
    with pytest.raises(HockeyModelError, match="level"):
        interval(empty_model, 3.0, level)


def test_a_level_whose_upper_tail_rounds_to_one_raises(empty_model: NhlGameModel) -> None:
    level = math.nextafter(1.0, 0.0)
    assert (1.0 + level) / 2.0 == 1.0
    with pytest.raises(HockeyModelError, match="rounds to 1.0"):
        interval(empty_model, 3.0, level)


def test_a_walk_that_cannot_reach_the_target_raises(
    empty_model: NhlGameModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nhl_model, "MAX_QUANTILE_STEPS", 2)
    with pytest.raises(HockeyModelError, match="no count up to 2"):
        interval(empty_model, 3.0, 0.8)


def test_the_step_limit_leaves_room_for_the_widest_supported_interval() -> None:
    assert log_cdf_quantile(MAX_RATE, 1.0 - 1e-12) < MAX_QUANTILE_STEPS / 2


def test_a_rate_below_the_floor_is_raised_to_the_floor(empty_model: NhlGameModel) -> None:
    result = interval(empty_model, 0.0, 0.8)
    assert result.point == 0.1


def test_an_error_names_the_game_and_the_side(empty_model: NhlGameModel) -> None:
    with pytest.raises(HockeyModelError) as caught:
        interval(empty_model, MAX_RATE + 1.0, 0.8)
    assert str(caught.value).startswith("game 100 TOR (home): goal interval: ")


@settings(max_examples=80, deadline=None)
@given(
    rate=st.floats(min_value=0.1, max_value=MAX_RATE),
    level=st.floats(min_value=0.01, max_value=0.99),
)
def test_the_interval_equals_the_log_space_cdf_for_any_rate_and_level(
    empty_model: NhlGameModel, rate: float, level: float
) -> None:
    result = interval(empty_model, rate, level)
    assert result.low == min(float(log_cdf_quantile(rate, (1.0 - level) / 2.0)), rate)
    assert result.high == max(float(log_cdf_quantile(rate, (1.0 + level) / 2.0)), rate)


_ANY_FLOAT = st.one_of(
    st.floats(min_value=-2.0, max_value=MAX_RATE + 5.0),
    st.sampled_from([math.nan, math.inf, -math.inf, 0.0, 1.0, MAX_RATE]),
)


@settings(max_examples=150, deadline=None)
@given(rate=_ANY_FLOAT, level=_ANY_FLOAT)
def test_the_interval_accepts_exactly_the_supported_rates_and_levels(
    empty_model: NhlGameModel, rate: float, level: float
) -> None:
    supported = 0.0 <= rate <= MAX_RATE and 0.0 < level < 1.0 and (1.0 + level) / 2.0 < 1.0
    if supported:
        result = interval(empty_model, rate, level)
        assert result.level == level
    else:
        with pytest.raises(HockeyModelError):
            interval(empty_model, rate, level)
