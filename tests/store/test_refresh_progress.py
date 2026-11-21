"""Tests for the progress lines of a refresh (#133)."""

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from refresh_support import NOW, FakeClient, minute_clock

from hockey_model.store.db import connect
from hockey_model.store.refresh import refresh


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
