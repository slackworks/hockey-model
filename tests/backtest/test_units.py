"""Tests for the backtest unit runner: order, failure names, timing lines, and read-only opens."""

import os
import sqlite3
import tempfile
import time
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.backtest import units
from hockey_model.backtest.units import (
    Unit,
    _pool_failure,
    _timed,
    database_path,
    open_read_only,
    run_units,
)
from hockey_model.game_model.imputation import ImputationLimitError
from hockey_model.sources.shape_errors import SourceShapeError
from hockey_model.types import HockeyModelError


def slow_value(delay: float, value: int) -> int:
    time.sleep(delay)
    return value


def square(value: int) -> int:
    return value * value


def boom(message: str) -> int:
    raise ValueError(message)


def refuse(message: str) -> int:
    raise ImputationLimitError(message)


def broken_database(message: str) -> int:
    raise sqlite3.OperationalError(message)


def die(code: int) -> int:
    os._exit(code)


def test_results_follow_the_order_of_the_units_when_the_last_unit_finishes_first() -> None:
    units = [Unit(f"u{i}", slow_value, (0.4 - 0.1 * i, i)) for i in range(4)]
    assert run_units(units, 4) == [0, 1, 2, 3]


@settings(max_examples=15, deadline=None)
@given(st.lists(st.integers(-1000, 1000), max_size=6), st.integers(1, 4))
def test_the_pool_gives_the_results_of_the_serial_run(values: list[int], workers: int) -> None:
    units = [Unit(f"u{i}", square, (value,)) for i, value in enumerate(values)]
    assert run_units(units, workers) == [value * value for value in values]


@pytest.mark.parametrize("workers", [1, 2])
def test_each_unit_logs_its_name_and_its_seconds(
    workers: int, capsys: pytest.CaptureFixture[str]
) -> None:
    run_units([Unit("alpha", square, (2,)), Unit("beta", square, (3,))], workers)
    lines = sorted(capsys.readouterr().err.splitlines())
    assert len(lines) == 2
    assert lines[0].startswith("backtest: unit alpha took ")
    assert lines[1].startswith("backtest: unit beta took ")
    assert all(line.endswith("s") for line in lines)


@pytest.mark.parametrize("workers", [1, 2])
def test_a_failed_unit_fails_the_run_with_its_name(workers: int) -> None:
    units = [Unit("fine", square, (2,)), Unit("broken", boom, ("bad input 7",))]
    with pytest.raises(HockeyModelError, match=r"unit 'broken' failed: ValueError: bad input 7"):
        run_units(units, workers)


@pytest.mark.parametrize("workers", [1, 2])
def test_an_error_of_this_package_keeps_its_type_and_gets_the_unit_name(workers: int) -> None:
    units = [Unit("fine", square, (2,)), Unit("fit elo", refuse, ("too many imputed rows",))]
    with pytest.raises(ImputationLimitError, match=r"unit 'fit elo': too many imputed rows"):
        run_units(units, workers)


@pytest.mark.parametrize("workers", [1, 2])
def test_a_sqlite_error_gets_the_unit_name_and_the_store_fix(workers: int) -> None:
    units = [Unit("fine", square, (2,)), Unit("build rows", broken_database, ("no such table: x",))]
    with pytest.raises(HockeyModelError, match=r"unit 'build rows' failed: no such table: x; "):
        run_units(units, workers)


def test_a_failed_unit_stops_the_running_workers_at_once() -> None:
    units = [Unit("long", slow_value, (60.0, 1)), Unit("broken", boom, ("bad",))]
    started = time.monotonic()
    with pytest.raises(HockeyModelError, match="unit 'broken'"):
        run_units(units, 2)
    assert time.monotonic() - started < 30


def test_a_worker_that_dies_names_the_unit_that_died() -> None:
    """The unit that killed its worker has no result, so the message names it.

    A second unit runs beside it, so the pool holds a future that is still running. Which of
    the two is still running when the pool breaks depends on the machine, so this test states
    only the unit that cannot have a result. The naming rule itself is stated without a pool
    in ``test_a_dead_worker_names_the_units_whose_results_did_not_come_back``.
    """
    units = [Unit("first", die, (3,)), Unit("second", slow_value, (30.0, 1))]
    with pytest.raises(HockeyModelError, match=r"a worker process died.*'first'"):
        run_units(units, 2)


def die_later(delay: float, code: int) -> int:
    time.sleep(delay)
    os._exit(code)


def test_a_worker_that_dies_names_no_unit_that_finished() -> None:
    units = [
        Unit("done", square, (2,)),
        Unit("dies", die_later, (1.0, 3)),
        Unit("slow", slow_value, (30.0, 1)),
    ]
    with pytest.raises(HockeyModelError, match="a worker process died") as caught:
        run_units(units, 3)
    assert "'dies'" in str(caught.value)
    assert "'done'" not in str(caught.value)


def unknown_team(code: str) -> int:
    raise SourceShapeError("espn", code)


def test_a_worker_error_with_its_own_arguments_comes_back_with_its_message() -> None:
    with pytest.raises(SourceShapeError, match="unit 'u'.*team code 'XYZ'") as caught:
        run_units([Unit("u", unknown_team, ("XYZ",)), Unit("v", square, (1,))], 2)
    assert "worker process died" not in str(caught.value)


def test_no_units_give_no_results() -> None:
    assert run_units([], 4) == []


@pytest.mark.parametrize("workers", [0, -1])
def test_a_worker_count_below_one_is_an_error(workers: int) -> None:
    with pytest.raises(HockeyModelError, match="workers must be at least 1"):
        run_units([Unit("u", square, (1,))], workers)


def test_a_read_only_connection_refuses_a_write(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    writer = sqlite3.connect(path)
    try:
        writer.execute("CREATE TABLE t (x INTEGER)")
        writer.commit()
    finally:
        writer.close()
    conn = open_read_only(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO t VALUES (1)")
    finally:
        conn.close()


# The characters that a file URI treats as syntax, and a few non-ASCII letters.
_PATH_CHARS = " %?#&=+;:@!~'()[]{}éß中_a1"


@settings(max_examples=40, deadline=None)
@given(st.text(alphabet=_PATH_CHARS, min_size=1, max_size=12))
def test_a_path_with_special_characters_opens_the_file_that_was_written(name: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / f"{name}.db"
        writer = sqlite3.connect(path)
        writer.execute("CREATE TABLE t (x INTEGER)")
        writer.execute("INSERT INTO t VALUES (42)")
        writer.commit()
        try:
            reader = open_read_only(database_path(writer))
            try:
                assert reader.execute("SELECT x FROM t").fetchone() == (42,)
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    reader.execute("INSERT INTO t VALUES (1)")
            finally:
                reader.close()
        finally:
            writer.close()


def test_a_read_only_open_of_a_missing_file_names_the_path_and_the_fix(tmp_path: Path) -> None:
    with pytest.raises(HockeyModelError) as caught:
        open_read_only(tmp_path / "absent.db")
    message = str(caught.value)
    assert message.startswith("backtest: a worker cannot open the store ")
    assert str(tmp_path / "absent.db") in message
    assert message.endswith("check that the file exists and that its directory is readable")


def test_the_database_path_is_the_file_of_the_connection(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    conn = sqlite3.connect(path)
    try:
        assert database_path(conn).resolve() == path.resolve()
    finally:
        conn.close()


def two_arguments(first: str, second: str) -> int:
    raise HockeyModelError(first, second)


def test_an_error_of_this_package_keeps_its_other_arguments() -> None:
    with pytest.raises(HockeyModelError) as caught:
        run_units([Unit("u", two_arguments, ("a", "b"))], 1)
    assert caught.value.args[1:] == ("b",)


def test_an_error_of_another_type_names_the_unit_and_the_fix_in_full() -> None:
    with pytest.raises(HockeyModelError) as caught:
        run_units([Unit("u", boom, ("bad",))], 1)
    assert str(caught.value) == (
        "backtest: unit 'u' failed: ValueError: bad; "
        "fix the cause named in this message, then run the backtest again"
    )


def missing_table(name: str) -> int:
    conn = sqlite3.connect(":memory:")
    try:
        return conn.execute(f"SELECT * FROM {name}").fetchone()[0]
    finally:
        conn.close()


def test_a_sqlite_error_carries_the_fix_that_fits_its_code() -> None:
    with pytest.raises(HockeyModelError) as caught:
        run_units([Unit("u", missing_table, ("absent",))], 1)
    assert "`hockey-model store upgrade`" in str(caught.value)
    assert "no known cause fits" not in str(caught.value)


def test_the_in_memory_refusal_says_what_to_run_instead() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(HockeyModelError) as caught:
            database_path(conn)
    finally:
        conn.close()
    assert str(caught.value) == (
        "backtest: the store connection has no file, so a worker cannot open it; "
        "run the backtest on a store file, or with --workers 1"
    )


def test_the_uncommitted_refusal_says_what_to_run_instead(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "s.db")
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(HockeyModelError) as caught:
            database_path(conn)
    finally:
        conn.close()
    assert str(caught.value) == (
        "backtest: the store connection has uncommitted changes, so a worker would not "
        "see them; commit the connection first, or run the backtest with --workers 1"
    )


class _InlinePool:
    """A stand-in for the process pool that runs each call at once and records its size."""

    sizes: list[int | None] = []

    def __init__(self, max_workers: int | None = None) -> None:
        self.sizes.append(max_workers)

    def __enter__(self) -> _InlinePool:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def submit(self, fn, *args):
        future: Future = Future()
        future.set_result(fn(*args))
        return future


@pytest.fixture
def inline_pool(monkeypatch: pytest.MonkeyPatch) -> type[_InlinePool]:
    _InlinePool.sizes = []
    monkeypatch.setattr(units, "ProcessPoolExecutor", _InlinePool)
    return _InlinePool


@pytest.mark.parametrize(("workers", "count", "size"), [(4, 3, 3), (2, 5, 2), (3, 3, 3)])
def test_the_pool_has_one_process_for_each_unit_up_to_the_worker_count(
    inline_pool: type[_InlinePool], workers: int, count: int, size: int
) -> None:
    run_units([Unit(f"u{i}", square, (i,)) for i in range(count)], workers)
    assert inline_pool.sizes == [size]


def test_one_unit_runs_in_this_process_with_no_pool(inline_pool: type[_InlinePool]) -> None:
    assert run_units([Unit("only", square, (5,))], 4) == [25]
    assert inline_pool.sizes == []


def test_the_elapsed_time_of_a_unit_is_the_difference_of_two_clock_readings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = iter([10.0, 12.5])
    monkeypatch.setattr(units, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    assert _timed(Unit("u", square, (3,))) == (2.5, 9)


def test_a_dead_worker_names_the_units_whose_results_did_not_come_back() -> None:
    """The message names the units outside the finished set, and no other unit.

    A unit whose result came back is finished, so a run must not report it as one that did
    not finish. The message states that rule here without a real pool, so no timing decides
    whether this test passes.
    """
    names = ["first", "second", "third"]
    units = [Unit(name, square, (2,)) for name in names]
    broken = BrokenProcessPool("A process in the process pool was terminated abruptly")
    message = str(_pool_failure(units, 0, broken, {0, 2}))
    assert "'second'" in message
    assert "'first'" not in message
    assert "'third'" not in message
