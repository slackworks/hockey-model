"""Tests for the per-URL, age-aware source staleness. They use frozen clocks and no network."""

import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from seeding import add_injury, record_injury_snapshot, utc

from hockey_model.sources.moneypuck import TEAM_GAMES_URL, goalie_games_url
from hockey_model.sources.nhl import score_day_url
from hockey_model.store.db import connect
from hockey_model.store.reads import injury_reads_before
from hockey_model.store.source_reads import (
    ACTIVE_FAILURE_WINDOW,
    KNOWN_SOURCES,
    LIVE_FEED_MAX_AGE,
    MONEYPUCK_MAX_AGE,
    record_read,
)
from hockey_model.store.staleness import stale_sources
from hockey_model.store.timestamps import iso_utc

AS_OF = utc("2026-01-19T16:00")
INJURIES = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
STARTERS = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates=20260119"
FACEOFF = "https://www.dailyfaceoff.com/starting-goalies"


def _fresh_everything(conn: sqlite3.Connection, skip: str = "") -> None:
    """One good read of the live resource of every source but ``skip``."""
    when = AS_OF - timedelta(hours=2)
    reads = [
        ("nhl_scores", score_day_url(AS_OF.date())),
        ("nhl_boxscores", "https://x/boxscore"),
        ("moneypuck", TEAM_GAMES_URL),
        ("moneypuck", goalie_games_url(2025)),
        ("espn_starters", STARTERS),
        ("espn_injuries", INJURIES),
        ("daily_faceoff", FACEOFF),
    ]
    for source, url in reads:
        if source != skip:
            record_read(conn, source, url, when)
    conn.commit()


def _reasons(conn: sqlite3.Connection, as_of: datetime = AS_OF) -> list[tuple[str, str]]:
    return stale_sources(conn, as_of)


def test_a_store_with_many_dated_urls_is_judged_quickly(store: sqlite3.Connection) -> None:
    # A backfilled store holds a URL for every score day and boxscore, and the report judges it
    # once for each game. A scan of the whole table for each URL took minutes on a real store.
    rows = [
        (
            "nhl_scores",
            f"https://x/score/{n}",
            iso_utc(AS_OF - timedelta(days=1, minutes=k)),
            1,
            None,
        )
        for n in range(6000)
        for k in range(3)
    ]
    store.executemany(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error) VALUES (?, ?, ?, ?, ?)", rows
    )
    store.commit()
    started = time.perf_counter()
    stale_sources(store, AS_OF)
    assert time.perf_counter() - started < 5.0


def test_nothing_is_stale_when_every_source_read_well(store: sqlite3.Connection) -> None:
    _fresh_everything(store)
    assert _reasons(store) == []


def test_a_source_never_read_is_reported_once(store: sqlite3.Connection) -> None:
    reasons = _reasons(store)
    assert sorted(source for source, _ in reasons) == sorted(KNOWN_SOURCES)
    assert ("moneypuck", "no read of it has worked yet") in reasons


def test_a_good_read_of_one_url_does_not_clear_a_failed_url(store: sqlite3.Connection) -> None:
    """The goalie file failed after the team file worked, so the source is not current."""
    _fresh_everything(store)
    goalie = goalie_games_url(2025)
    record_read(store, "moneypuck", goalie, AS_OF - timedelta(hours=1), error="HTTP 503")
    record_read(store, "moneypuck", TEAM_GAMES_URL, AS_OF - timedelta(minutes=30))
    store.commit()
    assert _reasons(store) == [("moneypuck", f"the newest read of {goalie} failed")]


def test_a_later_good_read_of_the_same_url_clears_the_failure(store: sqlite3.Connection) -> None:
    _fresh_everything(store)
    record_read(store, "espn_injuries", INJURIES, AS_OF - timedelta(hours=1), error="HTTP 503")
    record_read(store, "espn_injuries", INJURIES, AS_OF - timedelta(minutes=30))
    store.commit()
    assert _reasons(store) == []


@pytest.mark.parametrize(
    ("age", "stale"),
    [
        (LIVE_FEED_MAX_AGE - timedelta(seconds=1), False),
        (LIVE_FEED_MAX_AGE, False),
        (LIVE_FEED_MAX_AGE + timedelta(seconds=1), True),
    ],
)
def test_a_live_feed_goes_stale_just_past_its_age_limit(
    store: sqlite3.Connection, age: timedelta, stale: bool
) -> None:
    _fresh_everything(store, skip="espn_injuries")
    record_read(store, "espn_injuries", INJURIES, AS_OF - age)
    store.commit()
    flagged = [r for s, r in _reasons(store) if s == "espn_injuries"]
    assert bool(flagged) is stale
    if stale:
        assert "old; the limit is" in flagged[0]


def test_reads_after_the_cutoff_are_ignored(store: sqlite3.Connection) -> None:
    _fresh_everything(store)
    record_read(store, "espn_injuries", INJURIES, AS_OF + timedelta(minutes=1), error="HTTP 503")
    store.commit()
    assert _reasons(store) == []


@pytest.mark.parametrize(("failure_first", "stale"), [(True, False), (False, True)])
def test_at_one_instant_the_row_written_last_decides(
    store: sqlite3.Connection, failure_first: bool, stale: bool
) -> None:
    _fresh_everything(store)
    moment = AS_OF - timedelta(minutes=10)
    order = [("HTTP 503"), None] if failure_first else [None, "HTTP 503"]
    for error in order:
        record_read(store, "daily_faceoff", FACEOFF, moment, error=error)
    store.commit()
    assert bool([r for s, r in _reasons(store) if s == "daily_faceoff"]) is stale


def test_an_old_failed_day_is_retired_but_a_recent_one_is_not(store: sqlite3.Connection) -> None:
    _fresh_everything(store)
    old_day = (AS_OF - ACTIVE_FAILURE_WINDOW - timedelta(days=20)).date()
    recent_day = (AS_OF - timedelta(days=2)).date()
    record_read(store, "nhl_scores", score_day_url(old_day), AS_OF - timedelta(days=25), error="x")
    store.commit()
    assert _reasons(store) == []
    record_read(
        store, "nhl_scores", score_day_url(recent_day), AS_OF - timedelta(days=2), error="x"
    )
    store.commit()
    assert _reasons(store) == [
        ("nhl_scores", f"the newest read of {score_day_url(recent_day)} failed")
    ]


def test_a_finished_season_goalie_file_does_not_age_out(store: sqlite3.Connection) -> None:
    _fresh_everything(store)
    record_read(store, "moneypuck", goalie_games_url(2023), AS_OF - timedelta(days=200))
    store.commit()
    assert _reasons(store) == []


@pytest.mark.parametrize("url", [TEAM_GAMES_URL, goalie_games_url(2025)])
def test_the_live_moneypuck_files_go_stale_with_age(store: sqlite3.Connection, url: str) -> None:
    _fresh_everything(store)
    store.execute("DELETE FROM source_reads WHERE url = ?", (url,))
    record_read(store, "moneypuck", url, AS_OF - MONEYPUCK_MAX_AGE - timedelta(hours=1))
    store.commit()
    flagged = [r for s, r in _reasons(store) if s == "moneypuck"]
    assert len(flagged) == 1
    assert url in flagged[0]


def test_the_dated_sources_go_stale_when_no_day_was_read_recently(
    store: sqlite3.Connection,
) -> None:
    _fresh_everything(store)
    store.execute("DELETE FROM source_reads WHERE source = 'nhl_scores'")
    record_read(
        store,
        "nhl_scores",
        score_day_url(AS_OF.date()),
        AS_OF - LIVE_FEED_MAX_AGE - timedelta(hours=3),
    )
    store.commit()
    flagged = [r for s, r in _reasons(store) if s == "nhl_scores"]
    assert len(flagged) == 1
    assert "the newest good read of it is" in flagged[0]


class TestInjurySnapshotAge:
    def test_a_fresh_empty_list_is_current_and_names_no_one(
        self, store: sqlite3.Connection
    ) -> None:
        add_injury(store, "TOR", "Hurt Player", "Out", "2026-01-18T00:00")
        record_injury_snapshot(store, "2026-01-19T10:00")
        store.commit()
        assert injury_reads_before(store, AS_OF) == []
        assert [s for s, _ in _reasons(store) if s == "espn_injuries"] == []

    def test_a_stale_list_is_not_used_and_is_reported(self, store: sqlite3.Connection) -> None:
        # A player who healed after the old snapshot must not be treated as out.
        add_injury(store, "TOR", "Healed Player", "Out", "2026-01-17T00:00")
        store.commit()
        assert injury_reads_before(store, AS_OF) == []
        flagged = [r for s, r in _reasons(store) if s == "espn_injuries"]
        assert len(flagged) == 1
        assert "old; the limit is" in flagged[0]

    @pytest.mark.parametrize(
        ("age", "used"),
        [
            (LIVE_FEED_MAX_AGE - timedelta(seconds=1), True),
            (LIVE_FEED_MAX_AGE, True),
            (LIVE_FEED_MAX_AGE + timedelta(seconds=1), False),
        ],
    )
    def test_the_age_boundary(self, store: sqlite3.Connection, age: timedelta, used: bool) -> None:
        stamp = (AS_OF - age).strftime("%Y-%m-%dT%H:%M:%S")
        add_injury(store, "TOR", "Hurt Player", "Out", stamp)
        store.commit()
        assert bool(injury_reads_before(store, AS_OF)) is used


def test_an_abandoned_score_day_stays_reported_after_the_failure_window(
    store: sqlite3.Connection,
) -> None:
    _fresh_everything(store)
    day = (AS_OF - ACTIVE_FAILURE_WINDOW - timedelta(days=20)).date()
    for hours in (50, 20):
        record_read(
            store, "nhl_scores", score_day_url(day), AS_OF - timedelta(days=25, hours=hours), "x"
        )
    store.commit()
    assert _reasons(store) == [("nhl_scores", f"the newest read of {score_day_url(day)} failed")]


def test_a_current_moneypuck_file_that_was_never_read_is_reported(
    store: sqlite3.Connection,
) -> None:
    _fresh_everything(store)
    store.execute("DELETE FROM source_reads WHERE url = ?", (goalie_games_url(2025),))
    store.commit()
    assert _reasons(store) == [("moneypuck", f"no read of {goalie_games_url(2025)} has worked yet")]


def test_a_read_at_the_cutoff_is_not_known_yet(store: sqlite3.Connection) -> None:
    _fresh_everything(store)
    record_read(store, "daily_faceoff", FACEOFF, AS_OF, error="HTTP 503")
    store.commit()
    assert _reasons(store) == []


@settings(max_examples=25, deadline=None)
@given(st.lists(st.tuples(st.integers(1, 600), st.booleans()), max_size=8))
def test_a_newer_good_read_of_a_url_clears_every_failure_of_it(
    history: list[tuple[int, bool]],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        conn = connect(Path(directory) / "store.db")
        try:
            _fresh_everything(conn, skip="daily_faceoff")
            for minutes, ok in history:
                moment = AS_OF - timedelta(minutes=minutes + 5)
                record_read(conn, "daily_faceoff", FACEOFF, moment, error=None if ok else "x")
            # The newest read of the URL is a good one.
            record_read(conn, "daily_faceoff", FACEOFF, AS_OF - timedelta(minutes=1))
            conn.commit()
            assert [r for s, r in _reasons(conn) if s == "daily_faceoff"] == []
        finally:
            conn.close()
