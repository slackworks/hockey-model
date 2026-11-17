"""Tests for ``abandoned_urls``, the rule that names a known gap."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import takewhile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.store.db import connect
from hockey_model.store.source_reads import (
    ABANDON_AFTER_FAILED_READS,
    abandoned_urls,
    failed_since,
    failed_urls,
    record_read,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE = "nhl_scores"


def _record(conn: sqlite3.Connection, url: str, outcomes: list[bool]) -> None:
    for step, ok in enumerate(outcomes):
        record_read(conn, SOURCE, url, START + timedelta(days=step), None if ok else "HTTP 500")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


@settings(max_examples=20)
@given(outcomes=st.lists(st.booleans(), max_size=12))
def test_property_a_url_is_abandoned_after_enough_failures_since_its_last_good_read(
    outcomes: list[bool], tmp_path_factory: pytest.TempPathFactory
) -> None:
    connection = connect(tmp_path_factory.mktemp("store") / "store.db")
    _record(connection, "u", outcomes)
    trailing_failures = len(list(takewhile(lambda ok: not ok, reversed(outcomes))))
    expected = trailing_failures >= ABANDON_AFTER_FAILED_READS
    assert ("u" in abandoned_urls(connection, SOURCE)) == expected
    connection.close()


def test_a_good_read_clears_the_failures_before_it(conn: sqlite3.Connection) -> None:
    _record(conn, "u", [False, False, True, False])
    assert abandoned_urls(conn, SOURCE) == frozenset()


def test_failures_within_a_few_hours_do_not_abandon_the_url(conn: sqlite3.Connection) -> None:
    for minutes in (0, 5):
        record_read(conn, SOURCE, "u", START + timedelta(minutes=minutes), "HTTP 500")
    assert abandoned_urls(conn, SOURCE) == frozenset()


def test_another_source_does_not_abandon_the_url(conn: sqlite3.Connection) -> None:
    _record(conn, "u", [False, False])
    assert abandoned_urls(conn, "nhl_boxscores") == frozenset()


def test_a_good_read_at_the_same_time_as_a_failed_one_wins(conn: sqlite3.Connection) -> None:
    # One refresh takes one stamp, so two reads of a URL can share a fetch time. The later row
    # is the good one, and the URL must not count as failed.
    record_read(conn, SOURCE, "u", START, "HTTP 500")
    record_read(conn, SOURCE, "u", START)
    assert failed_urls(conn, SOURCE) == []
    assert failed_since(conn, SOURCE, "u", START - timedelta(days=1)) is False


def test_a_failed_read_at_the_same_time_as_a_good_one_wins(conn: sqlite3.Connection) -> None:
    record_read(conn, SOURCE, "u", START)
    record_read(conn, SOURCE, "u", START, "HTTP 500")
    assert failed_urls(conn, SOURCE) == ["u"]
    assert failed_since(conn, SOURCE, "u", START - timedelta(days=1)) is True


def test_failed_urls_names_only_the_newest_read_of_each_url(conn: sqlite3.Connection) -> None:
    _record(conn, "a", [True])
    _record(conn, "b", [False])
    _record(conn, "a", [False, False])
    record_read(conn, SOURCE, "a", START + timedelta(days=3))
    assert failed_urls(conn, SOURCE) == ["b"]
