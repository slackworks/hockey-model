"""Progress lines for long commands, on one logger and in one format (#133).

A long command runs inside ``step``. The step logs a ``start`` line, a ``tick`` line at a set
interval while it works, and an ``end`` line with the time it took. A step that stops early
logs a ``stop`` line with the reason. Library code calls only the logger, so a test or the
consumer API stays quiet until a caller installs a handler. ``cli.py`` installs one on stderr
with ``to_stderr``, so the JSON on stdout stays valid. ``scripts/run-step.sh`` writes the same
lines. Design: docs/design/progress-logging.md

A line has this form, where the last three parts are optional:

    progress COMMAND STEP EVENT elapsed=12.3s done=3/10 budget_left=60s reason=TEXT
"""

import logging
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import IO, Any

LOGGER = logging.getLogger("hockey_model.progress")
# Seconds between two tick lines of a step that is still running.
PROGRESS_INTERVAL = 60.0
# The text of a stop that gave no reason, so a stop line always has a reason after "reason=".
NO_REASON = "no reason given"


@dataclass(frozen=True, slots=True)
class Timing:
    """The seconds between ticks, the clock, and the timer maker. A test replaces the last two."""

    interval: float = PROGRESS_INTERVAL
    clock: Callable[[], float] = time.monotonic
    timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer


def _word(text: str, what: str) -> str:
    """Return ``text`` when it is one printable word. A line splits on spaces, so it must be."""
    if not text or not text.isprintable() or any(char.isspace() for char in text):
        raise ValueError(f"progress {what} {text!r} must be one printable word; use a dash")
    return text


def _one_line(text: str) -> str:
    """Return ``text`` on one line with no control character, or ``NO_REASON`` when it is empty."""
    printable = "".join(char if char.isprintable() or char.isspace() else " " for char in text)
    return " ".join(printable.split()) or NO_REASON


class Step:
    """One running step: its count, its budget, and the text of its progress lines."""

    def __init__(
        self,
        command: str,
        name: str,
        total: int | None,
        budget: float | None,
        clock: Callable[[], float],
    ) -> None:
        self.command = _word(command, "command")
        self.name = _word(name, "step name")
        self.total = total
        self.budget = budget
        self.done = 0
        self.reason: str | None = None
        self._clock = clock
        self._started = clock()

    def elapsed(self) -> float:
        """Return the seconds since the step started."""
        return self._clock() - self._started

    def line(self, event: str) -> str:
        """Return the progress line of ``event`` for the state of the step now."""
        elapsed = self.elapsed()
        parts = [f"progress {self.command} {self.name} {event} elapsed={elapsed:.1f}s"]
        if self.total is not None:
            parts.append(f"done={self.done}/{self.total}")
        elif self.done:
            parts.append(f"done={self.done}")
        if self.budget is not None:
            parts.append(f"budget_left={max(0.0, self.budget - elapsed):.0f}s")
        if self.reason is not None:
            parts.append(f"reason={self.reason}")
        return " ".join(parts)


_CURRENT: ContextVar[Step | None] = ContextVar("hockey_model_progress_step", default=None)


class _Heartbeat:
    """Log a tick line at each interval until it is cancelled.

    One lock covers the check, the log, and the next timer, so a tick never follows the ``end``
    line and a cancelled heartbeat never makes a new timer.
    """

    def __init__(
        self, current: Step, interval: float, factory: Callable[[float, Callable[[], None]], Any]
    ) -> None:
        self._step = current
        self._interval = interval
        self._factory = factory
        self._lock = threading.Lock()
        self._cancelled = False
        self._timer: Any = None

    def start(self) -> None:
        with self._lock:
            self._schedule()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._timer is not None:
                self._timer.cancel()

    def _schedule(self) -> None:
        self._timer = self._factory(self._interval, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            LOGGER.info(self._step.line("tick"))
            self._schedule()


@contextmanager
def step(
    command: str,
    name: str,
    *,
    total: int | None = None,
    budget: float | None = None,
    timing: Timing = Timing(),  # noqa: B008  # the value is frozen
) -> Generator[Step]:
    """Log the start, the ticks, and the end of one step of ``command``.

    ``total`` is the count of work units when it is known. ``budget`` is the seconds that the
    step may use, and each line shows what is left. ``command`` and ``name`` must be one word
    each, so a line splits on spaces. The heartbeat thread stops in a ``finally``, also when
    the step raises. A reason that ``stopped`` gave stays when the step raises.
    """
    current = Step(command, name, total, budget, timing.clock)
    token = _CURRENT.set(current)
    heartbeat = _Heartbeat(current, timing.interval, timing.timer_factory)
    try:
        LOGGER.info(current.line("start"))
        heartbeat.start()
        yield current
    except BaseException as error:
        if current.reason is None:
            current.reason = type(error).__name__
        raise
    finally:
        heartbeat.cancel()
        _CURRENT.reset(token)
        LOGGER.info(current.line("stop" if current.reason is not None else "end"))


def advance(count: int = 1) -> None:
    """Count ``count`` more finished units in the innermost running step, if there is one."""
    current = _CURRENT.get()
    if current is not None:
        current.done += count


def stopped(reason: str) -> None:
    """Say that the innermost running step stopped early, and why. The text becomes one line."""
    current = _CURRENT.get()
    if current is not None:
        current.reason = _one_line(reason)


@contextmanager
def to_stderr(stream: IO[str]) -> Generator[None]:
    """Write the progress lines to ``stream`` while the block runs, then restore the logger."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    level, propagate = LOGGER.level, LOGGER.propagate
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    try:
        yield
    finally:
        LOGGER.removeHandler(handler)
        LOGGER.setLevel(level)
        LOGGER.propagate = propagate
