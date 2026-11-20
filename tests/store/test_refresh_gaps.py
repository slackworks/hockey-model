"""Refresh and the known gaps: retry a skipped unit, stop MoneyPuck on the budget, name a game
that a boxscore write leaves listed as missing."""

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from refresh_support import (
    BOX_URL,
    ESPN,
    NOW,
    FakeClient,
    failing_days,
    minute_clock,
    score_requests,
)
from seeding import FETCHED

from hockey_model.sources.daily_faceoff import STARTERS_URL
from hockey_model.sources.errors import SourceError
from hockey_model.sources.http import HttpClient
from hockey_model.sources.moneypuck import TEAM_GAMES_URL, TeamGameRow, goalie_games_url
from hockey_model.store import refresh_nhl
from hockey_model.store.db import connect
from hockey_model.store.moneypuck_writes import write_team_games
from hockey_model.store.refresh import refresh, refresh_starters
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.refresh_nhl import read_boxscores
from hockey_model.store.refresh_other import _stop_for_time
from hockey_model.store.source_reads import abandoned_urls, record_read

GAME = 2025020697
BAD_BOX = f"{BOX_URL}{GAME}/boxscore"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _abandon_a_score_day(conn: sqlite3.Connection) -> dict:
    failing = failing_days("2025-11-01")
    for days in (0, 1):
        refresh(
            conn,
            FakeClient(failing),
            now=NOW + timedelta(days=days),
            backfill_from=date(2025, 11, 1),
        )
    return failing


def test_a_skipped_score_day_is_read_again_with_retry_skipped(conn: sqlite3.Connection) -> None:
    _abandon_a_score_day(conn)
    client = FakeClient()
    report = refresh(
        conn,
        client,
        now=NOW + timedelta(days=2),
        backfill_from=date(2025, 11, 1),
        retry_skipped=True,
    )
    assert "2025-11-01" in score_requests(client)
    assert report.errors == ()
    assert not any("skipped 2025-11-01" in n for n in report.notes)


def test_a_retry_that_works_clears_the_skip_for_later_refreshes(conn: sqlite3.Connection) -> None:
    _abandon_a_score_day(conn)
    refresh(
        conn,
        FakeClient(),
        now=NOW + timedelta(days=2),
        backfill_from=date(2025, 11, 1),
        retry_skipped=True,
    )
    assert abandoned_urls(conn, "nhl_scores") == frozenset()


def test_a_retry_that_fails_again_keeps_the_day_skipped(conn: sqlite3.Connection) -> None:
    failing = _abandon_a_score_day(conn)
    report = refresh(
        conn,
        FakeClient(failing),
        now=NOW + timedelta(days=2),
        backfill_from=date(2025, 11, 1),
        retry_skipped=True,
    )
    assert len(report.errors) == 1
    client = FakeClient(failing)
    later = refresh(conn, client, now=NOW + timedelta(days=3), backfill_from=date(2025, 11, 1))
    assert "2025-11-01" not in score_requests(client)
    assert later.errors == ()


def test_without_retry_skipped_the_day_stays_skipped(conn: sqlite3.Connection) -> None:
    _abandon_a_score_day(conn)
    client = FakeClient()
    refresh(conn, client, now=NOW + timedelta(days=2), backfill_from=date(2025, 11, 1))
    assert "2025-11-01" not in score_requests(client)


def test_a_skipped_boxscore_is_read_again_with_retry_skipped(conn: sqlite3.Connection) -> None:
    failing = FakeClient({BAD_BOX: SourceError("nhl", BAD_BOX, "HTTP 404")})
    refresh(conn, failing, now=NOW, backfill_from=date(2026, 1, 10))
    refresh(conn, failing, now=NOW + timedelta(days=2))
    skipped = FakeClient()
    refresh(conn, skipped, now=NOW + timedelta(days=4))
    assert BAD_BOX not in skipped.urls
    retry = FakeClient()
    refresh(conn, retry, now=NOW + timedelta(days=5), retry_skipped=True)
    assert BAD_BOX in retry.urls


def test_a_skipped_moneypuck_file_is_read_again_with_retry_skipped(
    conn: sqlite3.Connection,
) -> None:
    url = goalie_games_url(2014)
    for days in (0, 1):
        record_read(conn, "moneypuck", url, NOW - timedelta(days=3 - days), error="HTTP 503")
    conn.commit()
    assert url in abandoned_urls(conn, "moneypuck")
    client = FakeClient()
    refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 10), retry_skipped=True)
    assert url in client.urls


def _spent_budget(conn: sqlite3.Connection, client: HttpClient):
    """Run a refresh whose clock passes the budget before the MoneyPuck step."""
    return refresh(
        conn,
        client,
        now=NOW,
        backfill_from=date(2026, 1, 10),
        clock=minute_clock(),
        budget=timedelta(minutes=1),
    )


def test_a_spent_budget_reads_one_moneypuck_file_and_says_it_stopped(
    conn: sqlite3.Connection,
) -> None:
    client = FakeClient()
    report = _spent_budget(conn, client)
    assert TEAM_GAMES_URL in client.urls
    assert not any("peter-tanner" in url for url in client.urls)
    assert any("moneypuck:" in n and "time ran out" in n for n in report.notes)
    assert report.backfill_complete is False


def test_a_moneypuck_budget_stop_is_reported_as_a_time_stop(conn: sqlite3.Connection) -> None:
    # The report names the stop with the same flag as the NHL steps, so a caller reads one
    # contract and not a note. Without the flag, only the note says the budget did it.
    report = _spent_budget(conn, FakeClient())
    assert report.timed_out is True


def test_a_run_without_a_budget_is_not_a_time_stop(conn: sqlite3.Connection) -> None:
    report = refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert report.timed_out is False


def test_the_moneypuck_stop_sets_the_time_flag_on_the_run(conn: sqlite3.Connection) -> None:
    # A direct test of the unit, because a full refresh that reaches this stop also stops the
    # NHL step, so the report cannot tell the two stops apart.
    run = RefreshRun(conn, FakeClient(), NOW, deadline=NOW - timedelta(minutes=1))
    assert _stop_for_time(run, "the goalie file of season 2025", tried=1) is True
    assert run.timed_out is True


def test_the_first_file_of_a_run_is_always_tried(conn: sqlite3.Connection) -> None:
    run = RefreshRun(conn, FakeClient(), NOW, deadline=NOW - timedelta(minutes=1))
    assert _stop_for_time(run, "the goalie file of season 2025", tried=0) is False
    assert run.timed_out is False


def test_a_budget_that_lasts_leaves_moneypuck_alone(conn: sqlite3.Connection) -> None:
    client = FakeClient()
    report = refresh(
        conn,
        client,
        now=NOW,
        backfill_from=date(2026, 1, 10),
        clock=lambda: NOW,
        budget=timedelta(hours=1),
    )
    assert TEAM_GAMES_URL in client.urls
    assert not any("moneypuck:" in n and "time ran out" in n for n in report.notes)
    assert report.backfill_complete is True


def test_a_spent_budget_still_reads_the_live_goalie_sources(conn: sqlite3.Connection) -> None:
    client = FakeClient()
    _spent_budget(conn, client)
    assert any("scoreboard" in url for url in client.urls)
    assert any("dailyfaceoff" in url for url in client.urls)


def test_a_boxscore_written_but_still_missing_is_named(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write that stores no row stands for a boxscore that names a franchise by another code.
    monkeypatch.setattr(refresh_nhl, "write_boxscore", lambda *args, **kwargs: None)
    report = refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert any(
        "nhl_boxscores" in n and str(GAME) in n and "still listed as missing" in n
        for n in report.notes
    )
    # A game that never resolves must not hold the backfill open for ever.
    assert report.backfill_complete is True


def test_a_read_that_leaves_a_game_missing_is_not_progress(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    conn.execute("DELETE FROM goalie_games WHERE game_id = ?", (GAME,))
    conn.commit()
    monkeypatch.setattr(refresh_nhl, "write_boxscore", lambda *args, **kwargs: None)
    result = read_boxscores(RefreshRun(conn, FakeClient(), NOW), [GAME])
    assert result.progress == 0
    assert result.clean is True


def test_a_read_that_completes_an_old_game_is_progress(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    conn.execute("DELETE FROM goalie_games WHERE game_id = ?", (GAME,))
    conn.commit()
    result = read_boxscores(RefreshRun(conn, FakeClient(), NOW), [GAME])
    assert result.progress == 1
    assert read_boxscores(RefreshRun(conn, FakeClient(), NOW), [GAME], recent=[GAME]).progress == 0


def test_a_failing_moneypuck_run_is_not_complete(conn: sqlite3.Connection) -> None:
    overrides: dict[str, bytes | Exception] = {
        goalie_games_url(season): SourceError("moneypuck", "u", "HTTP 503")
        for season in range(2014, 2026)
    }
    report = refresh(conn, FakeClient(overrides), now=NOW, backfill_from=date(2026, 1, 10))
    assert any("stopped after 3 failed files" in n for n in report.notes)
    assert report.backfill_complete is False


def test_a_complete_boxscore_adds_no_such_note(conn: sqlite3.Connection) -> None:
    report = refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert not any("still listed as missing" in n for n in report.notes)


def test_one_game_is_read_once_in_a_refresh(conn: sqlite3.Connection) -> None:
    client = FakeClient()
    refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 10))
    run = RefreshRun(conn, client, NOW)
    first = client.urls.count(BAD_BOX)
    conn.execute("DELETE FROM goalie_games WHERE game_id = ?", (GAME,))
    conn.commit()
    read_boxscores(run, [GAME, GAME])
    assert client.urls.count(BAD_BOX) == first + 1


@settings(max_examples=25, deadline=None)
@given(
    retry=st.booleans(),
    failed=st.sets(st.sampled_from(["a", "b", "c"])),
)
def test_skipped_is_empty_with_retry_and_the_abandoned_set_without(
    tmp_path_factory: pytest.TempPathFactory, retry: bool, failed: set[str]
) -> None:
    store = connect(tmp_path_factory.mktemp("s") / "store.db")
    try:
        for url in failed:
            for days in (3, 1):
                record_read(store, "nhl_scores", url, NOW - timedelta(days=days), error="HTTP 503")
        store.commit()
        run = RefreshRun(store, FakeClient(), NOW, retry_skipped=retry)
        expected = frozenset() if retry else abandoned_urls(store, "nhl_scores")
        assert run.skipped("nhl_scores") == expected
        assert retry or expected == frozenset(failed)
    finally:
        store.close()


@settings(max_examples=25, deadline=None)
@given(
    ids=st.lists(st.integers(1, 6), max_size=12),
    read=st.sets(st.integers(1, 6)),
)
def test_the_worklist_drops_repeats_read_games_and_keeps_the_first_order(
    tmp_path_factory: pytest.TempPathFactory, ids: list[int], read: set[int]
) -> None:
    store = connect(tmp_path_factory.mktemp("s") / "store.db")
    try:
        run = RefreshRun(store, FakeClient(), NOW)
        run.boxscores_read.update(read)
        work = refresh_nhl._boxscore_worklist(run, ids)
        assert work == [g for g in dict.fromkeys(ids) if g not in read]
    finally:
        store.close()


@pytest.mark.parametrize(("retry", "expected"), [(False, False), (True, True)])
def test_an_abandoned_team_file_follows_the_same_skip_rule_as_a_goalie_file(
    conn: sqlite3.Connection, retry: bool, expected: bool
) -> None:
    # #204: the team file had an exemption with no recorded reason, so it was read on every run.
    for days in (0, 1):
        record_read(
            conn, "moneypuck", TEAM_GAMES_URL, NOW - timedelta(days=3 - days), error="HTTP 503"
        )
    conn.commit()
    assert TEAM_GAMES_URL in abandoned_urls(conn, "moneypuck")
    client = FakeClient()
    refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 10), retry_skipped=retry)
    assert (TEAM_GAMES_URL in client.urls) is expected


def _abandon_a_live_feed(conn: sqlite3.Connection, source: str, url: str) -> None:
    """Record the failed reads that make ``url`` a known gap for ``source``."""
    for days in (3, 1):
        record_read(conn, source, url, NOW - timedelta(days=days), error="HTTP 403")
    conn.commit()
    assert url in abandoned_urls(conn, source)


@pytest.mark.parametrize(
    ("source", "url"),
    [
        ("daily_faceoff", STARTERS_URL),
        ("espn_injuries", f"{ESPN}/injuries"),
        ("espn_starters", f"{ESPN}/scoreboard"),
    ],
)
def test_an_abandoned_live_feed_is_skipped_instead_of_failing_the_run(
    conn: sqlite3.Connection, source: str, url: str
) -> None:
    """A blocked IP address must not stop every later run.

    A live feed is one URL with no dated alternative, so a permanent block has no other way to
    become a known gap. Without the skip, refresh exits 1 on every run and the daily pipeline
    stops until a person clears it (#276).
    """
    _abandon_a_live_feed(conn, source, url)
    client = FakeClient()
    report = refresh_starters(conn, client, now=NOW)
    assert url not in client.urls
    assert report.errors == ()
    assert any(source in note and "skipped" in note for note in report.notes)


@pytest.mark.parametrize("source", ["daily_faceoff", "espn_injuries", "espn_starters"])
def test_retry_skipped_reads_an_abandoned_live_feed_again(
    conn: sqlite3.Connection, source: str
) -> None:
    url = {
        "daily_faceoff": STARTERS_URL,
        "espn_injuries": f"{ESPN}/injuries",
        "espn_starters": f"{ESPN}/scoreboard",
    }[source]
    _abandon_a_live_feed(conn, source, url)
    client = FakeClient()
    refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 10), retry_skipped=True)
    # One URL per day, so the gap key is the prefix that every request of the source starts with.
    assert any(request.startswith(url) for request in client.urls), client.urls


def test_a_live_feed_that_fails_once_is_not_yet_a_gap(conn: sqlite3.Connection) -> None:
    """A temporary outage must still fail the run, so the model does not hide a real failure."""
    url = STARTERS_URL
    record_read(conn, "daily_faceoff", url, NOW - timedelta(days=1), error="HTTP 503")
    conn.commit()
    client = FakeClient({url: SourceError("daily_faceoff", url, "HTTP 503")})
    report = refresh_starters(conn, client, now=NOW)
    assert url in client.urls
    assert len(report.errors) == 1


def test_a_refresh_names_the_moneypuck_rows_that_match_no_stored_game(
    conn: sqlite3.Connection,
) -> None:
    report = refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert "moneypuck: 10 team-game rows and 0 goalie-game rows match no stored game" in (
        report.notes
    )


def test_a_starters_only_refresh_does_not_note_moneypuck_rows(conn: sqlite3.Connection) -> None:
    write_team_games(
        conn, [TeamGameRow(999, "TOR", "2026-01-10", 2.5, 1.5, 50, 40, 30, 20)], FETCHED
    )
    report = refresh_starters(conn, FakeClient(), now=NOW)
    assert not any(n.startswith("moneypuck:") for n in report.notes)
