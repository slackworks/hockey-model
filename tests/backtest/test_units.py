"""Tests for the backtest unit runner: order, failure names, timing lines, and read-only opens."""

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.backtest.units import (
    Unit,
    database_path,
    open_read_only,
    run_units,
)
from hockey_model.game_model.imputation import ImputationLimitError
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


def test_a_worker_that_dies_names_the_unfinished_units() -> None:
    units = [Unit("first", die, (3,)), Unit("second", slow_value, (30.0, 1))]
    with pytest.raises(HockeyModelError, match=r"a worker process died.*'first'.*'second'"):
        run_units(units, 2)


def test_no_units_give_no_results() -> None:
    assert run_units([], 4) == []


@pytest.mark.parametrize("workers", [0, -1])
def test_a_worker_count_below_one_is_an_error(workers: int) -> None:
    with pytest.raises(HockeyModelError, match="workers must be at least 1"):
        run_units([Unit("u", square, (1,))], workers)


def test_a_read_only_connection_refuses_a_write(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
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
    with pytest.raises(HockeyModelError, match="cannot open the store .*absent.db.*check that"):
        open_read_only(tmp_path / "absent.db")


def test_the_database_path_is_the_file_of_the_connection(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    conn = sqlite3.connect(path)
    try:
        assert database_path(conn).resolve() == path.resolve()
    finally:
        conn.close()


def test_an_in_memory_connection_has_no_path_for_a_worker() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(HockeyModelError, match="no file"):
            database_path(conn)
    finally:
        conn.close()


def test_a_connection_with_uncommitted_changes_has_no_path_for_a_worker(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "s.db")
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(HockeyModelError, match="uncommitted changes.*commit the connection"):
            database_path(conn)
    finally:
        conn.close()
