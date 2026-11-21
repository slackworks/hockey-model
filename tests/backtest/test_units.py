"""Tests for the backtest unit runner: order, failure names, timing lines, and read-only opens."""

import io
import sqlite3
import time
from pathlib import Path

import pytest

from hockey_model.backtest.units import (
    Unit,
    database_path,
    open_read_only,
    run_units,
)
from hockey_model.types import HockeyModelError


def slow_value(delay: float, value: int) -> int:
    time.sleep(delay)
    return value


def boom(message: str) -> int:
    raise ValueError(message)


def test_results_follow_the_order_of_the_units_when_the_last_unit_finishes_first() -> None:
    units = [Unit(f"u{i}", slow_value, (0.4 - 0.1 * i, i)) for i in range(4)]
    assert run_units(units, 4, stream=io.StringIO()) == [0, 1, 2, 3]


@pytest.mark.parametrize("workers", [1, 2, 3])
def test_the_pool_and_the_serial_run_give_the_same_results(workers: int) -> None:
    units = [Unit(f"u{i}", slow_value, (0.0, i * i)) for i in range(5)]
    assert run_units(units, workers, stream=io.StringIO()) == [0, 1, 4, 9, 16]


def test_each_unit_logs_its_name_and_its_seconds() -> None:
    stream = io.StringIO()
    run_units(
        [Unit("alpha", slow_value, (0.0, 1)), Unit("beta", slow_value, (0.0, 2))], 2, stream=stream
    )
    lines = sorted(stream.getvalue().splitlines())
    assert len(lines) == 2
    assert lines[0].startswith("backtest: unit alpha took ")
    assert lines[1].startswith("backtest: unit beta took ")
    assert all(line.endswith("s") for line in lines)


@pytest.mark.parametrize("workers", [1, 2])
def test_a_failed_unit_fails_the_run_with_its_name(workers: int) -> None:
    units = [Unit("fine", slow_value, (0.0, 1)), Unit("broken", boom, ("bad input 7",))]
    with pytest.raises(HockeyModelError, match=r"unit 'broken' failed: ValueError: bad input 7"):
        run_units(units, workers, stream=io.StringIO())


def test_no_units_give_no_results() -> None:
    assert run_units([], 4, stream=io.StringIO()) == []


@pytest.mark.parametrize("workers", [0, -1])
def test_a_worker_count_below_one_is_an_error(workers: int) -> None:
    with pytest.raises(HockeyModelError, match="workers must be at least 1"):
        run_units([Unit("u", slow_value, (0.0, 1))], workers, stream=io.StringIO())


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
