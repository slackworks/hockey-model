"""Run the independent units of a backtest, in one process or in a pool of worker processes.

A unit is a named call with picklable arguments. ``run_units`` returns the results in the
order of the units, whatever order the workers finish in, so a merge of the results is the same
with one worker and with many. Each unit logs its wall time to stderr. A failed unit raises an
error that names the unit. Design: docs/design/backtest-speed.md
"""

import sqlite3
import sys
import time
import urllib.parse
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from hockey_model.types import HockeyModelError


@dataclass(frozen=True, slots=True)
class Unit:
    """One named call. ``fn`` MUST be a top-level function, so a worker can import it."""

    name: str
    fn: Callable[..., Any]
    args: tuple[Any, ...] = ()


def open_read_only(path: Path) -> sqlite3.Connection:
    """Open the store at ``path`` for reading only, so a worker can never write to it."""
    target = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    try:
        return sqlite3.connect(target, uri=True)
    except sqlite3.Error as exc:
        raise HockeyModelError(
            f"backtest: a worker cannot open the store {str(path)!r} for reading: {exc}; "
            "check that the file exists and that its directory is readable"
        ) from exc


def database_path(conn: sqlite3.Connection) -> Path:
    """Return the file of the main database of ``conn``, so a worker can open its own copy."""
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    raise HockeyModelError(
        "backtest: the store connection has no file, so a worker cannot open it; "
        "run the backtest on a store file with --workers 1, or on a saved store"
    )


def _timed(unit: Unit) -> tuple[float, Any]:
    started = time.monotonic()
    result = unit.fn(*unit.args)
    return time.monotonic() - started, result


def _failure(unit: Unit, exc: BaseException) -> HockeyModelError:
    return HockeyModelError(
        f"backtest: unit {unit.name!r} failed: {type(exc).__name__}: {exc}; "
        "fix the cause above, then run the backtest again"
    )


def _log(unit: Unit, seconds: float, stream: TextIO) -> None:
    print(f"backtest: unit {unit.name} took {seconds:.1f}s", file=stream)


def run_units(units: Sequence[Unit], workers: int, *, stream: TextIO | None = None) -> list[Any]:
    """Run ``units`` and return their results in the order of ``units``.

    With ``workers`` of 1 or one unit, every unit runs in this process, in order. Otherwise a
    pool of at most ``workers`` processes runs them at the same time.
    """
    if workers < 1:
        raise HockeyModelError(
            f"backtest: workers must be at least 1, got {workers}; pass 1 or more"
        )
    out = sys.stderr if stream is None else stream
    if workers == 1 or len(units) <= 1:
        return [_run_here(unit, out) for unit in units]
    results: list[Any] = [None] * len(units)
    with ProcessPoolExecutor(max_workers=min(workers, len(units))) as pool:
        futures = {pool.submit(_timed, unit): index for index, unit in enumerate(units)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                seconds, results[index] = future.result()
            except Exception as exc:
                for pending in futures:
                    pending.cancel()
                raise _failure(units[index], exc) from exc
            _log(units[index], seconds, out)
    return results


def _run_here(unit: Unit, stream: TextIO) -> Any:
    try:
        seconds, result = _timed(unit)
    except Exception as exc:
        raise _failure(unit, exc) from exc
    _log(unit, seconds, stream)
    return result
