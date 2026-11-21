"""The list of live feed gaps and its CLI command (#278)."""

import argparse
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model import live_gaps
from hockey_model.live_gaps import live_gap_report
from hockey_model.sources.errors import SourceError
from hockey_model.store.db import connect
from hockey_model.store.source_reads import (
    LIVE_FEED_MAX_AGE,
    LIVE_FEEDS,
    record_failed_read,
    record_read,
)
from hockey_model.store.timestamps import iso_utc
from hockey_model.store_cli import run_store_command
from hockey_model.types import HockeyModelError

URL = "https://example.test/feed"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
SOURCE = "daily_faceoff"


def _blocked() -> SourceError:
    return SourceError(SOURCE, URL, "HTTP 403", permanent=True)


def _gap_since(conn: sqlite3.Connection, first: datetime, source: str = SOURCE) -> None:
    """Record two permanent failures, so the URL is a gap from ``first``."""
    record_failed_read(conn, source, URL, first, _blocked())
    record_failed_read(conn, source, URL, first + timedelta(minutes=4), _blocked())
    conn.commit()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def test_an_empty_store_has_no_gap(conn: sqlite3.Connection) -> None:
    assert live_gap_report(conn, NOW) == {
        "limit_hours": LIVE_FEED_MAX_AGE.total_seconds() / 3600,
        "gaps": [],
    }


def test_a_gap_younger_than_the_limit_is_listed_and_not_over(conn: sqlite3.Connection) -> None:
    first = NOW - timedelta(hours=5)
    _gap_since(conn, first)
    assert live_gap_report(conn, NOW)["gaps"] == [
        {
            "source": SOURCE,
            "url": URL,
            "gap_since": iso_utc(first),
            "age_hours": 5.0,
            "last_ok": None,
            "over_limit": False,
        }
    ]


def test_a_gap_older_than_the_limit_is_over_it(conn: sqlite3.Connection) -> None:
    _gap_since(conn, NOW - LIVE_FEED_MAX_AGE - timedelta(minutes=1))
    assert [g["over_limit"] for g in live_gap_report(conn, NOW)["gaps"]] == [True]


def test_a_gap_exactly_as_old_as_the_limit_is_not_over_it(conn: sqlite3.Connection) -> None:
    _gap_since(conn, NOW - LIVE_FEED_MAX_AGE)
    assert [g["over_limit"] for g in live_gap_report(conn, NOW)["gaps"]] == [False]


def test_a_good_read_after_the_failures_heals_the_gap(conn: sqlite3.Connection) -> None:
    _gap_since(conn, NOW - timedelta(days=3))
    record_read(conn, SOURCE, URL, NOW - timedelta(hours=1))
    conn.commit()
    assert live_gap_report(conn, NOW)["gaps"] == []


def test_the_last_good_read_before_the_failures_is_named(conn: sqlite3.Connection) -> None:
    good = NOW - timedelta(days=4)
    record_read(conn, SOURCE, URL, good)
    _gap_since(conn, NOW - timedelta(days=3))
    assert [g["last_ok"] for g in live_gap_report(conn, NOW)["gaps"]] == [iso_utc(good)]


def test_a_gap_of_a_dated_source_is_not_listed(conn: sqlite3.Connection) -> None:
    _gap_since(conn, NOW - timedelta(days=3), source="nhl_scores")
    assert live_gap_report(conn, NOW)["gaps"] == []


def test_every_live_feed_is_listed_in_source_order(conn: sqlite3.Connection) -> None:
    for source in reversed(LIVE_FEEDS):
        _gap_since(conn, NOW - timedelta(days=2), source=source)
    assert [g["source"] for g in live_gap_report(conn, NOW)["gaps"]] == list(LIVE_FEEDS)


def test_a_fetched_at_that_is_not_a_time_fails_with_context(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error, permanent)"
        " VALUES (?, ?, ?, 0, 'HTTP 403', 1)",
        [(SOURCE, URL, "not a time"), (SOURCE, URL, "not a time either")],
    )
    conn.commit()
    with pytest.raises(HockeyModelError, match=f"read the gaps of {SOURCE}.*not a time"):
        live_gap_report(conn, NOW)


def test_a_payload_that_breaks_the_schema_is_refused() -> None:
    payload = {"limit_hours": 24, "gaps": [{"source": "nhl_scores"}]}
    with pytest.raises(HockeyModelError, match="live_gaps.schema.json"):
        live_gaps._check(payload)


@settings(max_examples=30, deadline=None)
@given(hours=st.integers(0, 24 * 10), minutes=st.integers(0, 59))
def test_a_gap_is_over_the_limit_exactly_when_it_is_older(
    tmp_path_factory: pytest.TempPathFactory, hours: int, minutes: int
) -> None:
    store = connect(tmp_path_factory.mktemp("store") / "store.db")
    try:
        age = timedelta(hours=hours, minutes=minutes)
        _gap_since(store, NOW - age)
        [gap] = live_gap_report(store, NOW)["gaps"]
        assert gap["over_limit"] is (age > LIVE_FEED_MAX_AGE)
        assert gap["age_hours"] == round(age.total_seconds() / 3600, 2)
    finally:
        store.close()


def _args(db: Path, *, live: bool = True) -> argparse.Namespace:
    return argparse.Namespace(command="store", store_command="gaps", db=str(db), live=live)


def test_the_command_prints_the_report_of_the_store(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _gap_since(conn, datetime.now(UTC) - timedelta(days=3))
    payload, code = run_store_command(_args(tmp_path / "store.db"))
    assert code == 0
    assert isinstance(payload, dict)
    assert [(g["source"], g["over_limit"]) for g in payload["gaps"]] == [(SOURCE, True)]


def test_the_command_names_the_flag_when_none_is_given(tmp_path: Path) -> None:
    with pytest.raises(HockeyModelError, match="pass --live"):
        run_store_command(_args(tmp_path / "store.db", live=False))


def test_the_command_does_not_create_a_missing_store(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"
    with pytest.raises(HockeyModelError, match="no store file at .*absent.db"):
        run_store_command(_args(absent))
    assert not absent.exists()
