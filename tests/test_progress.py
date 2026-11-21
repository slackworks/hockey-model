"""Tests for the progress lines that long commands write to stderr (#133)."""

import logging
import re
import sys
from collections.abc import Callable, Iterator

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model import progress


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeTimer:
    """A timer that never runs on its own. A test fires it by hand."""

    def __init__(self, interval: float, function: Callable[[], None]) -> None:
        self.interval = interval
        self.function = function
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class Timers:
    def __init__(self) -> None:
        self.made: list[FakeTimer] = []

    def __call__(self, interval: float, function: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(interval, function)
        self.made.append(timer)
        return timer

    def fire_last(self) -> None:
        self.made[-1].function()


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def timing(clock: FakeClock, timers: Timers, interval: float = 60.0) -> progress.Timing:
    return progress.Timing(interval=interval, clock=clock, timer_factory=timers)


@pytest.fixture
def lines() -> Iterator[list[str]]:
    handler = ListHandler()
    progress.LOGGER.addHandler(handler)
    progress.LOGGER.setLevel(logging.INFO)
    yield handler.lines
    progress.LOGGER.removeHandler(handler)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def timers() -> Timers:
    return Timers()


def test_a_step_logs_its_start_and_its_end_with_the_time_it_took(
    lines: list[str], clock: FakeClock, timers: Timers
) -> None:
    with progress.step("fit", "run", timing=timing(clock, timers)):
        clock.now += 12.34
    assert lines == [
        "progress fit run start elapsed=0.0s",
        "progress fit run end elapsed=12.3s",
    ]


def test_a_counted_step_shows_done_over_total_and_the_budget_left(
    lines: list[str], clock: FakeClock, timers: Timers
) -> None:
    with progress.step("refresh", "days", total=10, budget=100.0, timing=timing(clock, timers)):
        progress.advance(3)
        clock.now += 40.0
        timers.fire_last()
    assert lines[0] == "progress refresh days start elapsed=0.0s done=0/10 budget_left=100s"
    assert lines[1] == "progress refresh days tick elapsed=40.0s done=3/10 budget_left=60s"
    assert lines[2] == "progress refresh days end elapsed=40.0s done=3/10 budget_left=60s"


def test_the_budget_left_never_goes_below_zero(
    lines: list[str], clock: FakeClock, timers: Timers
) -> None:
    with progress.step("refresh", "days", budget=5.0, timing=timing(clock, timers)):
        clock.now += 50.0
    assert lines[-1].endswith("budget_left=0s")


def test_a_step_without_a_total_shows_the_count_alone(
    lines: list[str], clock: FakeClock, timers: Timers
) -> None:
    with progress.step("backtest", "units", timing=timing(clock, timers)):
        progress.advance()
    assert lines[-1] == "progress backtest units end elapsed=0.0s done=1"


def test_the_heartbeat_ticks_again_and_stops_when_the_step_ends(
    lines: list[str], clock: FakeClock, timers: Timers
) -> None:
    with progress.step("fit", "run", timing=timing(clock, timers, 30.0)):
        clock.now += 30.0
        timers.fire_last()
        clock.now += 30.0
        timers.fire_last()
    ticks = [line for line in lines if " tick " in line]
    assert ticks == [
        "progress fit run tick elapsed=30.0s",
        "progress fit run tick elapsed=60.0s",
    ]
    assert [timer.interval for timer in timers.made] == [30.0, 30.0, 30.0]
    assert all(timer.started and timer.daemon for timer in timers.made)
    assert timers.made[-1].cancelled
    before = len(lines)
    timers.made[-1].function()
    assert len(lines) == before


def test_a_step_that_raises_logs_a_stop_line_and_cancels_the_heartbeat(
    lines: list[str], clock: FakeClock, timers: Timers
) -> None:
    with pytest.raises(ZeroDivisionError):
        with progress.step("fit", "run", timing=timing(clock, timers)):
            clock.now += 2.0
            raise ZeroDivisionError("boom")
    assert lines[-1] == "progress fit run stop elapsed=2.0s reason=ZeroDivisionError"
    assert timers.made[-1].cancelled


def test_a_step_that_names_a_reason_ends_with_a_stop_line(
    lines: list[str], clock: FakeClock, timers: Timers
) -> None:
    with progress.step("refresh", "days", timing=timing(clock, timers)):
        progress.stopped("the time ran out\n at 2026-01-01")
    assert (
        lines[-1] == "progress refresh days stop elapsed=0.0s reason=the time ran out at 2026-01-01"
    )


def test_advance_counts_in_the_innermost_step(
    lines: list[str], clock: FakeClock, timers: Timers
) -> None:
    with progress.step("a", "outer", total=5, timing=timing(clock, timers)):
        progress.advance()
        with progress.step("a", "inner", total=2, timing=timing(clock, timers)):
            progress.advance(2)
        progress.advance()
    assert "progress a inner end elapsed=0.0s done=2/2" in lines
    assert lines[-1] == "progress a outer end elapsed=0.0s done=2/5"


def test_advance_and_stopped_outside_a_step_do_nothing() -> None:
    progress.advance(5)
    progress.stopped("no step")


@pytest.mark.parametrize("name", ["", "two words", "tab\there"])
def test_a_step_name_with_a_space_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="step name"):
        with progress.step("fit", name):
            pass


def test_a_library_call_is_silent_when_no_handler_is_installed(
    capsys: pytest.CaptureFixture[str], clock: FakeClock, timers: Timers
) -> None:
    with progress.step("fit", "run", timing=timing(clock, timers)):
        progress.advance()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_stderr_handler_writes_the_lines_and_installs_once(
    capsys: pytest.CaptureFixture[str], clock: FakeClock, timers: Timers
) -> None:
    progress.install_stderr_handler(sys.stderr)
    progress.install_stderr_handler(sys.stderr)
    try:
        with progress.step("fit", "run", timing=timing(clock, timers)):
            pass
    finally:
        progress.remove_stderr_handler()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "progress fit run start elapsed=0.0s",
        "progress fit run end elapsed=0.0s",
    ]


_LINE = re.compile(
    r"progress (\S+) (\S+) (start|tick|stop|end) elapsed=([0-9.]+)s"
    r"( done=[0-9]+(/[0-9]+)?)?( budget_left=[0-9]+s)?( reason=.+)?"
)


@given(
    done=st.integers(min_value=0, max_value=1000),
    total=st.one_of(st.none(), st.integers(min_value=0, max_value=1000)),
    budget=st.one_of(st.none(), st.floats(min_value=0.0, max_value=1e6)),
    seconds=st.floats(min_value=0.0, max_value=1e5),
)
def test_every_line_has_the_one_format(
    done: int, total: int | None, budget: float | None, seconds: float
) -> None:
    handler = ListHandler()
    progress.LOGGER.addHandler(handler)
    progress.LOGGER.setLevel(logging.INFO)
    clock = FakeClock()
    try:
        with progress.step(
            "cmd", "step", total=total, budget=budget, timing=timing(clock, Timers())
        ):
            progress.advance(done)
            clock.now += seconds
    finally:
        progress.LOGGER.removeHandler(handler)
    assert len(handler.lines) == 2
    for line in handler.lines:
        assert _LINE.fullmatch(line), line
