"""Run the independent units of a backtest, in one process or in a pool of worker processes.

A unit is a named call with picklable arguments. ``run_units`` returns the results in the
order of the units, whatever order the workers finish in, so a merge of the results is the same
with one worker and with many. Each unit logs its wall time to stderr. A failed unit stops the
other workers at once and raises an error that names the unit. Design:
docs/design/backtest-speed.md
"""

import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hockey_model.store.db import read_only_uri, sqlite_failure_fix
from hockey_model.types import HockeyModelError


@dataclass(frozen=True, slots=True)
class Unit:
    """One named call. ``fn`` MUST be a top-level function, so a worker can import it."""

    name: str
    fn: Callable[..., Any]
    args: tuple[Any, ...] = ()


def open_read_only(path: Path) -> sqlite3.Connection:
    """Open the store at ``path`` for reading only, so a worker can never write to it."""
    try:
        return sqlite3.connect(read_only_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise HockeyModelError(
            f"backtest: a worker cannot open the store {str(path)!r} for reading: {exc}; "
            "check that the file exists and that its directory is readable"
        ) from exc


def database_path(conn: sqlite3.Connection) -> Path:
    """Return the file of the main database of ``conn``, so a worker can open its own copy.

    A worker sees only committed rows. A connection with an open transaction is refused,
    because its uncommitted rows would be missing from every worker.
    """
    if conn.in_transaction:
        raise HockeyModelError(
            "backtest: the store connection has uncommitted changes, so a worker would not "
            "see them; commit the connection first, or run the backtest with --workers 1"
        )
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    raise HockeyModelError(
        "backtest: the store connection has no file, so a worker cannot open it; "
        "run the backtest on a store file, or with --workers 1"
    )


def _timed(unit: Unit) -> tuple[float, Any]:
    started = time.monotonic()
    result = unit.fn(*unit.args)
    return time.monotonic() - started, result


def _failure(unit: Unit, exc: BaseException) -> Exception:
    """Return the error to raise for a failed unit, with the unit name in its message.

    An error of this package keeps its type, because a caller may catch it. A SQLite error gets
    the fix that the CLI gives it. Any other error becomes a named ``HockeyModelError``.
    """
    if isinstance(exc, HockeyModelError):
        exc.args = (f"backtest unit {unit.name!r}: {exc}", *exc.args[1:])
        return exc
    if isinstance(exc, sqlite3.Error):
        return HockeyModelError(
            f"backtest: unit {unit.name!r} failed: {exc}; {sqlite_failure_fix(exc)}"
        )
    return HockeyModelError(
        f"backtest: unit {unit.name!r} failed: {type(exc).__name__}: {exc}; "
        "fix the cause named in this message, then run the backtest again"
    )


def _pool_failure(units: Sequence[Unit], index: int, exc: BaseException) -> Exception:
    if isinstance(exc, BrokenProcessPool):
        names = ", ".join(repr(unit.name) for unit in units)
        return HockeyModelError(
            f"backtest: a worker process died, probably out of memory; the units {names} did "
            f"not finish: {exc}; run with fewer workers, or with --workers 1"
        )
    return _failure(units[index], exc)


def run_units(units: Sequence[Unit], workers: int) -> list[Any]:
    """Run ``units`` and return their results in the order of ``units``.

    With ``workers`` of 1 or one unit, every unit runs in this process, in order. Otherwise a
    pool of at most ``workers`` processes runs them at the same time. The first failure stops
    the running workers and raises.
    """
    if workers < 1:
        raise HockeyModelError(
            f"backtest: workers must be at least 1, got {workers}; pass 1 or more"
        )
    if workers == 1 or len(units) <= 1:
        return [_run_here(unit) for unit in units]
    results: list[Any] = [None] * len(units)
    with ProcessPoolExecutor(max_workers=min(workers, len(units))) as pool:
        futures = {pool.submit(_timed, unit): index for index, unit in enumerate(units)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                seconds, results[index] = future.result()
            except Exception as exc:
                # A running build takes close to an hour, so the pool must not wait for it.
                pool.terminate_workers()
                raise _pool_failure(units, index, exc) from exc
            _log(units[index], seconds)
    return results


def _log(unit: Unit, seconds: float) -> None:
    print(f"backtest: unit {unit.name} took {seconds:.1f}s", file=sys.stderr)


def _run_here(unit: Unit) -> Any:
    try:
        seconds, result = _timed(unit)
    except Exception as exc:
        raise _failure(unit, exc) from exc
    _log(unit, seconds)
    return result
