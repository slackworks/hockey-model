"""Property tests for the win-range pieces of the daily report (#212)."""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.game import GamePrediction
from hockey_model.report_interval import away_interval, interval_json, win_cell
from hockey_model.types import Projection

GOALS = Projection(3.0, 2.0, 4.0, 0.8)
_UNIT = st.floats(min_value=0.0, max_value=1.0)


def _prediction(interval: tuple[float, float] | None, point: float = 0.5) -> GamePrediction:
    return GamePrediction(point, 1.0 - point, GOALS, GOALS, (), (), home_win_interval=interval)


@st.composite
def _ranges(draw: st.DrawFn) -> tuple[float, float, float]:
    """Return ``(low, point, high)`` with ``low <= point <= high``."""
    low = draw(_UNIT)
    point = low + (1.0 - low) * draw(_UNIT)
    high = min(1.0, point + (1.0 - point) * draw(_UNIT))
    return low, point, max(point, high)


@given(_ranges())
def test_the_away_range_is_the_flip_of_the_home_range(values: tuple[float, float, float]) -> None:
    low, point, high = values
    flipped = away_interval(_prediction((low, high), point))
    assert flipped is not None
    assert flipped[0] <= flipped[1]
    assert (1.0 - flipped[1], 1.0 - flipped[0]) == pytest.approx((low, high), abs=1e-12)


@given(_ranges())
def test_the_json_range_holds_the_same_numbers_as_the_two_ranges(
    values: tuple[float, float, float],
) -> None:
    low, point, high = values
    prediction = _prediction((low, high), point)
    flipped = away_interval(prediction)
    assert flipped is not None
    assert interval_json(prediction) == {
        "home_low": low,
        "home_high": high,
        "away_low": flipped[0],
        "away_high": flipped[1],
    }


def test_a_prediction_without_a_range_gives_none_everywhere() -> None:
    assert away_interval(_prediction(None)) is None
    assert away_interval(None) is None
    assert interval_json(_prediction(None)) is None


@given(point=_UNIT, interval=st.one_of(st.none(), _ranges()))
def test_the_cell_shows_the_point_and_a_range_that_holds_it(
    point: float, interval: tuple[float, float, float] | None
) -> None:
    assert win_cell(None, None) == "-"
    if interval is None:
        assert win_cell(point, None) == f"{point:.1%}"
        return
    low, _, high = interval
    cell = win_cell(point, (low, high))
    match = re.fullmatch(r"([0-9.]+)% \(([0-9.]+)%\u2013([0-9.]+)%\)", cell)
    assert match is not None, cell
    assert cell.startswith(f"{point:.1%} (")
    assert float(match[2]) <= float(match[3])
