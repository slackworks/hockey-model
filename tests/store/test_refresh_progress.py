"""Tests for the progress lines of a refresh (#133)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from refresh_support import NOW, FakeClient, minute_clock

from hockey_model.store.db import connect
from hockey_model.store.refresh import refresh
from hockey_model.store.refresh_context import RefreshRun


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _of(lines: list[str], kind: str, event: str) -> list[str]:
    return [line for line in lines if f" refresh {kind}[" in line and f"] {event} " in line]


def test_a_backfill_window_logs_its_date_range_and_the_count_read(
    conn: sqlite3.Connection, progress_lines: list[str]
) -> None:
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    start = _of(progress_lines, "score-days", "start")
    end = _of(progress_lines, "score-days", "end")
    assert len(start) == 1 and len(end) == 1
    assert "score-days[2026-01-10..2026-01-" in start[0]
    assert " done=0/" in start[0]
    done, total = end[0].split(" done=")[1].split()[0].split("/")
    assert done == total != "0"
    boxes = _of(progress_lines, "boxscores", "end")
    assert len(boxes) == 1 and " done=14/14" in boxes[0]


def test_a_refresh_without_a_backfill_logs_the_score_days_and_the_boxscores(
    conn: sqlite3.Connection, progress_lines: list[str]
) -> None:
    refresh(conn, FakeClient(), now=NOW)
    assert len(_of(progress_lines, "score-days", "end")) == 1
    assert len(_of(progress_lines, "boxscores", "end")) == 1


def test_a_budget_shows_the_time_left_and_a_stop_names_its_reason(
    conn: sqlite3.Connection, progress_lines: list[str]
) -> None:
    refresh(
        conn,
        FakeClient(),
        now=NOW,
        backfill_from=date(2024, 6, 1),
        clock=minute_clock(),
        budget=timedelta(minutes=5),
    )
    starts = _of(progress_lines, "score-days", "start")
    assert all("budget_left=" in line for line in starts)
    stops = _of(progress_lines, "score-days", "stop") + _of(progress_lines, "boxscores", "stop")
    assert stops, progress_lines
    assert "reason=" in stops[0] and "the time ran out" in stops[0]


def test_a_refresh_with_no_budget_shows_no_budget_left(
    conn: sqlite3.Connection, progress_lines: list[str]
) -> None:
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert not any("budget_left" in line for line in progress_lines)


def test_a_refresh_is_silent_when_no_handler_is_installed(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_moneypuck_files_run_in_a_counted_step(
    conn: sqlite3.Connection, progress_lines: list[str]
) -> None:
    refresh(conn, FakeClient(), now=NOW)
    ends = _of(progress_lines, "moneypuck", "end")
    assert len(ends) == 1 and "moneypuck[goalie-files] end" in ends[0]
    done, total = ends[0].split(" done=")[1].split()[0].split("/")
    assert done == total and int(total) > 1


def test_a_budget_stop_of_the_moneypuck_files_names_its_reason(
    conn: sqlite3.Connection, progress_lines: list[str]
) -> None:
    refresh(conn, FakeClient(), now=NOW, clock=minute_clock(), budget=timedelta(minutes=2))
    stops = _of(progress_lines, "moneypuck", "stop")
    assert len(stops) == 1
    assert "reason=moneypuck: stopped before the goalie file of season" in stops[0]
    assert "because the time ran out" in stops[0]


_MEMORY = sqlite3.connect(":memory:")


def _run(deadline: datetime | None, last_stamp: datetime | None) -> RefreshRun:
    run = RefreshRun(_MEMORY, FakeClient(), NOW, deadline=deadline)
    run.last_stamp = last_stamp
    return run


_TIMES = st.datetimes(
    min_value=datetime(2026, 1, 1), max_value=datetime(2026, 12, 31), timezones=st.just(UTC)
)


@given(deadline=st.one_of(st.none(), _TIMES), stamp=st.one_of(st.none(), _TIMES))
def test_the_seconds_left_follow_the_deadline_and_the_last_stamp(
    deadline: datetime | None, stamp: datetime | None
) -> None:
    left = _run(deadline, stamp).seconds_left()
    if deadline is None:
        assert left is None
        return
    assert left is not None and left >= 0.0
    assert left == max(0.0, (deadline - (stamp or NOW)).total_seconds())


@given(deadline=_TIMES, first=_TIMES, gap=st.integers(min_value=0, max_value=10_000))
def test_the_seconds_left_never_grow_when_the_stamp_moves_later(
    deadline: datetime, first: datetime, gap: int
) -> None:
    earlier = _run(deadline, first).seconds_left()
    later = _run(deadline, first + timedelta(seconds=gap)).seconds_left()
    assert earlier is not None and later is not None and later <= earlier


def test_the_seconds_left_do_not_read_the_clock() -> None:
    reads: list[int] = []

    def clock() -> datetime:
        reads.append(1)
        return NOW

    run = RefreshRun(_MEMORY, FakeClient(), NOW, clock=clock, deadline=NOW + timedelta(hours=1))
    assert run.seconds_left() == 3600.0
    assert reads == []
