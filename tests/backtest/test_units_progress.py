"""Tests for the progress lines of the backtest unit runner (#133)."""

import pytest

from hockey_model.backtest.units import Unit, run_units
from hockey_model.types import HockeyModelError


def square(value: int) -> int:
    return value * value


def boom(message: str) -> int:
    raise ValueError(message)


@pytest.mark.parametrize("workers", [1, 2])
def test_the_units_log_a_start_line_and_an_end_line_with_the_count(
    progress_lines: list[str], workers: int
) -> None:
    run_units([Unit(f"u{i}", square, (i,)) for i in range(3)], workers)
    assert progress_lines[0] == "progress backtest units start elapsed=0.0s done=0/3"
    assert progress_lines[-1].startswith("progress backtest units end elapsed=")
    assert progress_lines[-1].endswith("done=3/3")


def test_a_failed_unit_logs_a_stop_line_with_the_error(progress_lines: list[str]) -> None:
    with pytest.raises(HockeyModelError):
        run_units([Unit("ok", square, (2,)), Unit("bad", boom, ("x",))], 1)
    assert progress_lines[-1].startswith("progress backtest units stop elapsed=")
    assert progress_lines[-1].endswith("done=1/2 reason=HockeyModelError")
