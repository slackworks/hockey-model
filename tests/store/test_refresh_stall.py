"""Tests for the budget stall error of a backfill: it needs old work that is still unread (#302)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from refresh_support import NOW, FakeClient, minute_clock
from seeding import add_game

from hockey_model.sources.errors import SourceError
from hockey_model.sources.nhl import boxscore_url, score_day_url
from hockey_model.store.db import connect
from hockey_model.store.refresh import refresh
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.refresh_nhl import BOXSCORES, SCORES
from hockey_model.store.refresh_old_work import old_work_left
from hockey_model.store.source_reads import record_failed_read, record_read

CUTOFF = date(2026, 1, 8)
FIRST = date(2026, 1, 1)
LATE = datetime(2026, 1, 12, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _run(conn: sqlite3.Connection) -> RefreshRun:
    return RefreshRun(conn, FakeClient(), NOW, backfill_from=FIRST)


def _read_every_old_day(conn: sqlite3.Connection) -> None:
    day = FIRST
    while day < CUTOFF:
        record_read(conn, SCORES, score_day_url(day), LATE)
        day += timedelta(days=1)


def test_an_old_day_without_a_complete_read_is_old_work(conn: sqlite3.Connection) -> None:
    assert old_work_left(_run(conn), FIRST, CUTOFF, set()) is True


def test_a_known_gap_day_is_not_old_work(conn: sqlite3.Connection) -> None:
    gaps = {FIRST + timedelta(days=n) for n in range((CUTOFF - FIRST).days)}
    assert old_work_left(_run(conn), FIRST, CUTOFF, gaps) is False


def test_old_days_that_were_read_after_they_closed_are_not_old_work(
    conn: sqlite3.Connection,
) -> None:
    _read_every_old_day(conn)
    assert old_work_left(_run(conn), FIRST, CUTOFF, set()) is False


def test_an_old_game_without_a_boxscore_is_old_work(conn: sqlite3.Connection) -> None:
    _read_every_old_day(conn)
    add_game(conn, 2025020001, "2026-01-03T00:00:00Z")
    assert old_work_left(_run(conn), FIRST, CUTOFF, set()) is True


def test_a_game_on_a_day_from_the_cutoff_on_is_not_old_work(conn: sqlite3.Connection) -> None:
    _read_every_old_day(conn)
    add_game(conn, 2025020002, "2026-01-08T20:00:00Z")
    assert old_work_left(_run(conn), FIRST, CUTOFF, set()) is False


def test_a_boxscore_that_failed_a_short_time_ago_is_not_old_work(
    conn: sqlite3.Connection,
) -> None:
    _read_every_old_day(conn)
    add_game(conn, 2025020003, "2026-01-03T00:00:00Z")
    failure = SourceError("nhl", boxscore_url(2025020003), "HTTP 500")
    record_failed_read(conn, BOXSCORES, boxscore_url(2025020003), NOW, failure)
    assert old_work_left(_run(conn), FIRST, CUTOFF, set()) is False


@pytest.mark.parametrize("later", [timedelta(0), timedelta(days=1)])
def test_a_budget_stop_before_an_older_window_is_an_error_that_names_the_budget(
    conn: sqlite3.Connection, later: timedelta
) -> None:
    # The first window holds a year. A run that spends its budget on the days that are open or
    # closed since the last run never reaches the older window (#178, #186).
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2025, 1, 1))
    now = NOW + later
    report = refresh(
        conn,
        FakeClient(),
        now=now,
        backfill_from=date(2024, 6, 1),
        clock=minute_clock(now),
        budget=timedelta(minutes=1),
    )
    assert report.backfill_complete is False
    assert any("--budget-seconds" in str(error) for error in report.errors)


def test_a_budget_stop_with_only_recent_days_left_is_not_an_error(
    conn: sqlite3.Connection,
) -> None:
    start = date(2026, 1, 6)
    refresh(conn, FakeClient(), now=NOW, backfill_from=start)
    report = refresh(
        conn,
        FakeClient(),
        now=NOW,
        backfill_from=start,
        clock=minute_clock(),
        budget=timedelta(minutes=1),
    )
    assert report.errors == ()
    assert report.backfill_complete is False
    assert any("run it again" in note for note in report.notes)
