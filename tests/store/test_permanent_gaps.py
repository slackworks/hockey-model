"""The gap rule for a streak of permanent failures (#279)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hockey_model.sources.errors import SourceError
from hockey_model.sources.http import HttpClient
from hockey_model.store.db import connect
from hockey_model.store.recording import Reading
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.source_reads import (
    ABANDON_MIN_SPAN_DAYS,
    abandoned_urls,
    known_gaps,
    record_failed_read,
    record_read,
)

SOURCE = "espn_starters"
URL = "https://example.test/feed"
START = datetime(2026, 11, 20, 16, 44, tzinfo=UTC)
MINUTES = timedelta(minutes=4)


def _blocked() -> SourceError:
    return SourceError(SOURCE, URL, "HTTP 403", permanent=True)


def _outage() -> SourceError:
    return SourceError(SOURCE, URL, "HTTP 503 after 4 attempts")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _stored(conn: sqlite3.Connection) -> list[object]:
    return [row[0] for row in conn.execute("SELECT permanent FROM source_reads ORDER BY rowid")]


def test_two_permanent_failures_a_few_minutes_apart_make_a_gap(conn: sqlite3.Connection) -> None:
    record_failed_read(conn, SOURCE, URL, START, _blocked())
    record_failed_read(conn, SOURCE, URL, START + MINUTES, _blocked())
    assert abandoned_urls(conn, SOURCE) == frozenset({URL})


def test_one_permanent_failure_is_not_a_gap(conn: sqlite3.Connection) -> None:
    record_failed_read(conn, SOURCE, URL, START, _blocked())
    assert abandoned_urls(conn, SOURCE) == frozenset()


def test_two_transient_failures_a_few_minutes_apart_are_not_a_gap(
    conn: sqlite3.Connection,
) -> None:
    record_failed_read(conn, SOURCE, URL, START, _outage())
    record_failed_read(conn, SOURCE, URL, START + MINUTES, _outage())
    assert abandoned_urls(conn, SOURCE) == frozenset()


@pytest.mark.parametrize("first_is_permanent", [True, False])
def test_a_streak_with_one_transient_failure_keeps_the_span_rule(
    conn: sqlite3.Connection, first_is_permanent: bool
) -> None:
    first, second = (_blocked(), _outage()) if first_is_permanent else (_outage(), _blocked())
    record_failed_read(conn, SOURCE, URL, START, first)
    record_failed_read(conn, SOURCE, URL, START + MINUTES, second)
    assert abandoned_urls(conn, SOURCE) == frozenset()
    record_failed_read(conn, SOURCE, URL, START + timedelta(days=ABANDON_MIN_SPAN_DAYS), _blocked())
    assert abandoned_urls(conn, SOURCE) == frozenset({URL})


def test_a_row_with_no_stored_cause_counts_as_transient(conn: sqlite3.Connection) -> None:
    record_read(conn, SOURCE, URL, START, "HTTP 403")
    record_read(conn, SOURCE, URL, START + MINUTES, "HTTP 403")
    assert _stored(conn) == [None, None]
    assert abandoned_urls(conn, SOURCE) == frozenset()


def test_a_good_read_between_permanent_failures_ends_the_streak(conn: sqlite3.Connection) -> None:
    record_failed_read(conn, SOURCE, URL, START, _blocked())
    record_read(conn, SOURCE, URL, START + MINUTES)
    record_failed_read(conn, SOURCE, URL, START + 2 * MINUTES, _blocked())
    assert abandoned_urls(conn, SOURCE) == frozenset()


def test_a_permanent_failure_of_another_url_does_not_join_the_streak(
    conn: sqlite3.Connection,
) -> None:
    record_failed_read(conn, SOURCE, URL, START, _blocked())
    record_failed_read(conn, SOURCE, URL + "2", START + MINUTES, _blocked())
    assert abandoned_urls(conn, SOURCE) == frozenset()


def test_a_failure_stores_its_cause(conn: sqlite3.Connection) -> None:
    record_failed_read(conn, SOURCE, URL, START, _blocked())
    record_failed_read(conn, SOURCE, URL, START + MINUTES, _outage())
    record_failed_read(conn, SOURCE, URL, START + 2 * MINUTES, ValueError("bad body"))
    record_read(conn, SOURCE, URL, START + 3 * MINUTES)
    assert _stored(conn) == [1, 0, 0, None]
    errors = [row[0] for row in conn.execute("SELECT error FROM source_reads ORDER BY rowid")]
    assert errors == [
        f"fetch {SOURCE} {URL}: HTTP 403",
        f"fetch {SOURCE} {URL}: HTTP 503 after 4 attempts",
        "bad body",
        None,
    ]


def test_the_gap_starts_at_the_first_failure_after_the_last_good_read(
    conn: sqlite3.Connection,
) -> None:
    record_failed_read(conn, SOURCE, URL, START, _blocked())
    record_read(conn, SOURCE, URL, START + MINUTES)
    record_failed_read(conn, SOURCE, URL, START + 2 * MINUTES, _blocked())
    record_failed_read(conn, SOURCE, URL, START + 3 * MINUTES, _blocked())
    assert known_gaps(conn, SOURCE) == {URL: START + 2 * MINUTES}


def test_no_failure_means_no_gap(conn: sqlite3.Connection) -> None:
    assert known_gaps(conn, SOURCE) == {}


def _fails_with(error: SourceError):
    def unit(stamp: datetime) -> int:
        raise error

    return unit


def _run_at(conn: sqlite3.Connection, moment: datetime, error: SourceError) -> RefreshRun:
    run = RefreshRun(conn, HttpClient(), moment)
    assert run.attempt(SOURCE, URL, _fails_with(error), Reading("x:1")) is False
    return run


def test_the_run_after_two_blocked_runs_skips_the_feed(conn: sqlite3.Connection) -> None:
    """Two real 403 failures four minutes apart made no gap before, so a slate was lost."""
    _run_at(conn, START, _blocked())
    assert RefreshRun(conn, HttpClient(), START).skipped(SOURCE) == frozenset()
    _run_at(conn, START + MINUTES, _blocked())
    assert RefreshRun(conn, HttpClient(), START + MINUTES).skipped(SOURCE) == frozenset({URL})


def test_two_outage_runs_a_few_minutes_apart_leave_no_gap(conn: sqlite3.Connection) -> None:
    _run_at(conn, START, _outage())
    _run_at(conn, START + MINUTES, _outage())
    assert RefreshRun(conn, HttpClient(), START + MINUTES).skipped(SOURCE) == frozenset()


@settings(max_examples=60, deadline=None)
@given(
    causes=st.lists(st.sampled_from(["permanent", "transient", "good"]), min_size=1, max_size=8),
    gap_minutes=st.integers(1, 600),
)
def test_a_gap_needs_two_failures_in_a_row_and_a_span_or_only_permanent_ones(
    tmp_path_factory: pytest.TempPathFactory, causes: list[str], gap_minutes: int
) -> None:
    connection = connect(tmp_path_factory.mktemp("store") / "store.db")
    try:
        step = timedelta(minutes=gap_minutes)
        for number, cause in enumerate(causes):
            moment = START + number * step
            if cause == "good":
                record_read(connection, SOURCE, URL, moment)
            else:
                failure = _blocked() if cause == "permanent" else _outage()
                record_failed_read(connection, SOURCE, URL, moment, failure)
        first = len(causes)
        while first > 0 and causes[first - 1] != "good":
            first -= 1
        streak = causes[first:]
        spans = (len(streak) - 1) * step >= timedelta(days=ABANDON_MIN_SPAN_DAYS)
        permanent_count = streak.count("permanent")
        expected = len(streak) >= 2 and (spans or permanent_count >= 2)
        gaps = known_gaps(connection, SOURCE)
        assert (URL in gaps) is expected
        if expected:
            assert gaps[URL] == START + first * step
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("causes", "is_gap"),
    [
        (["permanent", "permanent", "transient"], True),
        (["transient", "permanent", "permanent"], True),
        (["permanent", "transient", "permanent", "permanent"], True),
        (["permanent", "transient", "transient"], False),
    ],
)
def test_one_transient_read_does_not_end_a_permanent_streak(
    tmp_path: Path, causes: list[str], is_gap: bool
) -> None:
    connection = connect(tmp_path / "store.db")
    try:
        for number, cause in enumerate(causes):
            moment = START + number * MINUTES
            failure = _blocked() if cause == "permanent" else _outage()
            record_failed_read(connection, SOURCE, URL, moment, failure)
        assert (URL in known_gaps(connection, SOURCE)) is is_gap
    finally:
        connection.close()


@settings(max_examples=60, deadline=None)
@given(
    causes=st.lists(st.sampled_from(["permanent", "transient"]), min_size=1, max_size=6),
    extra=st.sampled_from(["permanent", "transient"]),
)
def test_one_more_failed_read_keeps_a_gap(
    tmp_path_factory: pytest.TempPathFactory, causes: list[str], extra: str
) -> None:
    connection = connect(tmp_path_factory.mktemp("store") / "store.db")
    try:
        for number, cause in enumerate(causes):
            failure = _blocked() if cause == "permanent" else _outage()
            record_failed_read(connection, SOURCE, URL, START + number * MINUTES, failure)
        assume(URL in known_gaps(connection, SOURCE))
        failure = _blocked() if extra == "permanent" else _outage()
        record_failed_read(connection, SOURCE, URL, START + len(causes) * MINUTES, failure)
        assert URL in known_gaps(connection, SOURCE)
    finally:
        connection.close()
