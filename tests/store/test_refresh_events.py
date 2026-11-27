"""Refresh reads the play-by-play and the shift chart of each final game (#35)."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from refresh_support import NOW, FakeClient, fixture
from seeding import add_game

from hockey_model.sources.errors import SourceError
from hockey_model.sources.nhl import boxscore_url
from hockey_model.sources.nhl_events import pbp_url
from hockey_model.sources.nhl_shifts import shifts_url
from hockey_model.store import refresh_context, refresh_events, refresh_revisit
from hockey_model.store.db import StoreError, connect
from hockey_model.store.refresh import refresh
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.refresh_events import events, read_events
from hockey_model.store.refresh_nhl import read_boxscores
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS, abandoned_urls

SCORED = 14
GAME = 2025020709
BOX = boxscore_url(GAME)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _games(conn: sqlite3.Connection, table: str) -> set[int]:
    return {row[0] for row in conn.execute(f"SELECT DISTINCT game_id FROM {table}")}


def _observations(conn: sqlite3.Connection, source: str, game_id: int = GAME) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM observations WHERE source = ? AND key = ?", (source, str(game_id))
    ).fetchone()[0]


def _status(conn: sqlite3.Connection, game_id: int = GAME) -> list[tuple]:
    return conn.execute(
        "SELECT status, checked_at FROM event_status WHERE game_id = ?", (game_id,)
    ).fetchall()


def test_a_refresh_stores_the_events_and_shifts_of_every_final_game(
    conn: sqlite3.Connection,
) -> None:
    report = refresh(conn, FakeClient(), now=NOW)
    assert report.errors == ()
    assert len(_games(conn, "pbp_events")) == SCORED
    assert _games(conn, "shifts") == _games(conn, "pbp_events")


def test_a_refresh_reconciles_each_game_it_read_events_for(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=NOW)
    assert _games(conn, "event_status") == _games(conn, "pbp_events")
    assert {row[0] for row in conn.execute("SELECT status FROM event_status")} <= {
        "provisional",
        "incomplete",
    }


def test_a_game_with_events_is_not_read_again_in_the_same_window(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=NOW)
    client = FakeClient()
    refresh(conn, client, now=NOW + timedelta(hours=1))
    assert pbp_url(GAME) not in client.urls
    assert shifts_url(GAME) not in client.urls


def test_a_game_is_read_again_at_the_revisit_days(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=NOW)
    refresh(conn, FakeClient(), now=NOW + timedelta(days=3))
    assert _observations(conn, NHL_PBP) == 2
    assert _observations(conn, NHL_SHIFTS) == 2
    assert len(_status(conn)) == 1


def test_a_failed_play_by_play_stores_nothing_for_its_game(conn: sqlite3.Connection) -> None:
    failing: dict[str, bytes | Exception] = {
        pbp_url(GAME): SourceError("nhl_pbp", pbp_url(GAME), "HTTP 503")
    }
    report = refresh(conn, FakeClient(failing), now=NOW)
    assert report.errors == ()
    assert any(n.startswith("nhl_pbp: ") and "HTTP 503" in n for n in report.notes)
    assert GAME not in _games(conn, "pbp_events")
    assert GAME in _games(conn, "shifts")
    assert _status(conn) == []
    assert len(_games(conn, "pbp_events")) == SCORED - 1


def test_a_payload_for_another_game_is_a_failed_unit(conn: sqlite3.Connection) -> None:
    other = json.loads(fixture("nhl/pbp_2025020709.json"))
    other["id"] = GAME + 1
    report = refresh(conn, FakeClient({pbp_url(GAME): json.dumps(other).encode()}), now=NOW)
    assert report.errors == ()
    assert any(n.startswith("nhl_pbp: ") for n in report.notes)
    assert GAME not in _games(conn, "pbp_events")


def _reconciled_game(conn: sqlite3.Connection) -> None:
    """Store the boxscore and the events of one game, read long after its last revisit day."""
    add_game(conn, GAME, "2026-01-10T01:00:00.000000+00:00", home="EDM", away="LAK", score=(3, 4))
    conn.execute("UPDATE games SET last_period = 'SO'")
    served: dict[str, bytes | Exception] = {
        BOX: fixture("nhl/boxscore_2025020709.json"),
        pbp_url(GAME): fixture("nhl/pbp_2025020709.json"),
        shifts_url(GAME): fixture("nhl/shifts_2025020709.json"),
    }
    run = RefreshRun(conn, FakeClient(served), NOW + timedelta(days=20))
    read_boxscores(run, [GAME])
    read_events(run, NHL_PBP, [GAME])
    read_events(run, NHL_SHIFTS, [GAME])
    assert run.errors == []


def test_a_new_boxscore_revision_changes_the_stored_status(conn: sqlite3.Connection) -> None:
    _reconciled_game(conn)
    assert [row[0] for row in _status(conn)] == ["reconciled"]
    body = json.loads(fixture("nhl/boxscore_2025020709.json"))
    for side in body["playerByGameStats"].values():
        for group in side.values():
            for entry in group:
                if entry.get("goals"):
                    entry["goals"] += 1
    later = RefreshRun(conn, FakeClient({BOX: json.dumps(body).encode()}), NOW + timedelta(days=21))
    read_boxscores(later, [GAME])
    assert later.errors == []
    assert [row[0] for row in _status(conn)] == ["incomplete"]


def test_a_new_boxscore_revision_that_cannot_reconcile_removes_the_status(
    conn: sqlite3.Connection,
) -> None:
    _reconciled_game(conn)
    conn.execute("UPDATE observations SET fetched_at = 'garbage' WHERE source = ?", (NHL_PBP,))
    later = RefreshRun(
        conn, FakeClient({BOX: fixture("nhl/boxscore_2025020709.json")}), NOW + timedelta(days=21)
    )
    read_boxscores(later, [GAME])
    assert later.errors == []
    assert sum("garbage" in note for note in later.notes) == 1
    assert _status(conn) == []


def _failing(url: str) -> dict[str, bytes | Exception]:
    return {url: SourceError("nhl_pbp", url, "HTTP 503")}


def test_three_failed_units_in_a_row_stop_the_source(conn: sqlite3.Connection) -> None:
    failing: dict[str, bytes | Exception] = {
        pbp_url(game_id): SourceError("nhl_pbp", pbp_url(game_id), "HTTP 503")
        for game_id in range(2025020697, 2025020711)
    }
    report = refresh(conn, FakeClient(failing), now=NOW)
    assert report.errors == ()
    assert sum(n.startswith("nhl_pbp: ") and "HTTP 503" in n for n in report.notes) == 3
    assert any("nhl_pbp: stopped after 3 failed reads; 11 not tried" in n for n in report.notes)
    assert _games(conn, "pbp_events") == set()
    assert len(_games(conn, "shifts")) == SCORED


def test_a_spent_time_budget_stops_the_read(conn: sqlite3.Connection) -> None:
    run = RefreshRun(conn, FakeClient(), NOW, deadline=NOW)
    read_events(run, NHL_PBP, [GAME])
    assert run.timed_out
    assert any("time ran out; 1 not tried" in note for note in run.notes)
    assert _games(conn, "pbp_events") == set()


def test_a_game_whose_reads_failed_twice_is_a_known_gap(conn: sqlite3.Connection) -> None:
    failing = _failing(pbp_url(GAME))
    refresh(conn, FakeClient(failing), now=NOW)
    refresh(conn, FakeClient(failing), now=NOW + timedelta(days=1))
    client = FakeClient()
    report = refresh(conn, client, now=NOW + timedelta(days=2))
    assert pbp_url(GAME) not in client.urls
    assert GAME not in _games(conn, "pbp_events")
    assert "nhl_pbp: 1 game(s) skipped, because every read failed" in report.notes


def test_a_failed_read_is_not_tried_again_within_a_day(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(_failing(pbp_url(GAME))), now=NOW)
    client = FakeClient()
    report = refresh(conn, client, now=NOW + timedelta(hours=2))
    assert pbp_url(GAME) not in client.urls
    waited = "nhl_pbp: 1 game(s) skipped, because the last read failed a day ago or less"
    assert waited in report.notes


def test_a_revisit_bound_names_the_games_left_for_the_next_refresh(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh(conn, FakeClient(), now=NOW)
    monkeypatch.setattr(refresh_revisit, "MAX_REVISITS", 2)
    report = refresh(conn, FakeClient(), now=NOW + timedelta(days=3))
    assert any("12 revisit(s) wait for the next refresh" in n for n in report.notes)
    assert _observations(conn, NHL_PBP, 2025020697) == 2
    assert _observations(conn, NHL_PBP, 2025020710) == 1


@pytest.mark.parametrize("source", [NHL_PBP, NHL_SHIFTS])
def test_a_unit_keeps_its_rows_when_the_game_cannot_reconcile(
    conn: sqlite3.Connection, source: str
) -> None:
    _reconciled_game(conn)
    conn.execute("UPDATE boxscore_revisions SET fetched_at = 'garbage'")
    served: dict[str, bytes | Exception] = {
        pbp_url(GAME): fixture("nhl/pbp_2025020709.json"),
        shifts_url(GAME): fixture("nhl/shifts_2025020709.json"),
    }
    run = RefreshRun(conn, FakeClient(served), NOW + timedelta(days=21))
    read_events(run, source, [GAME])
    assert _observations(conn, source) == 2
    assert run.errors == []
    assert sum("garbage" in note for note in run.notes) == 1
    assert _status(conn) == []


def test_the_derive_hook_refuses_to_run_before_the_unit(conn: sqlite3.Connection) -> None:
    run = RefreshRun(conn, FakeClient(), NOW)
    _, reading = refresh_events._unit(run, NHL_SHIFTS, GAME)
    assert reading.derive is not None
    with pytest.raises(StoreError, match=rf"derive nhl_shifts {GAME}: .*no parsed rows"):
        reading.derive(conn, 1)


def _both() -> dict[str, bytes | Exception]:
    return {
        pbp_url(GAME): fixture("nhl/pbp_2025020709.json"),
        shifts_url(GAME): fixture("nhl/shifts_2025020709.json"),
    }


def test_a_game_without_a_status_is_reconciled_again_in_the_next_refresh(
    conn: sqlite3.Connection,
) -> None:
    _reconciled_game(conn)
    (good,) = conn.execute("SELECT DISTINCT fetched_at FROM boxscore_revisions").fetchall()
    conn.execute("UPDATE boxscore_revisions SET fetched_at = 'garbage'")
    first = RefreshRun(conn, FakeClient(_both()), NOW + timedelta(days=21))
    read_events(first, NHL_PBP, [GAME])
    assert _status(conn) == []
    assert sum("garbage" in note for note in first.notes) == 1
    again = RefreshRun(conn, FakeClient(), NOW + timedelta(days=22))
    events(again)
    assert _status(conn) == []
    assert sum("garbage" in note for note in again.notes) == 1
    conn.execute("UPDATE boxscore_revisions SET fetched_at = ?", good)
    last = RefreshRun(conn, FakeClient(), NOW + timedelta(days=23))
    events(last)
    assert [row[0] for row in _status(conn)] == ["reconciled"]
    assert last.notes == []


def _unpublished(source: str) -> dict[str, bytes | Exception]:
    """The feed of a game that the NHL has not published yet: no shift, or no play."""
    if source == NHL_SHIFTS:
        return {shifts_url(GAME): b'{"data": [], "total": 0}'}
    body = json.loads(fixture("nhl/pbp_2025020709.json"))
    body["plays"] = []
    return {pbp_url(GAME): json.dumps(body).encode()}


@pytest.mark.parametrize("source", [NHL_PBP, NHL_SHIFTS])
def test_a_feed_that_is_not_published_yet_never_becomes_a_known_gap(
    conn: sqlite3.Connection, source: str
) -> None:
    url = (pbp_url if source == NHL_PBP else shifts_url)(GAME)
    for days in range(4):
        report = refresh(conn, FakeClient(_unpublished(source)), now=NOW + timedelta(days=days))
        assert report.errors == ()
        assert any("may not" in note and str(GAME) in note for note in report.notes)
    assert url not in abandoned_urls(conn, source)
    client = FakeClient()
    refresh(conn, client, now=NOW + timedelta(days=4))
    assert url in client.urls
    assert GAME in _games(conn, "pbp_events" if source == NHL_PBP else "shifts")


@pytest.mark.parametrize("failure", [RuntimeError("bug in a check"), sqlite3.IntegrityError("dup")])
def test_a_bug_in_the_reconcile_code_leaves_the_boxscore_read_intact(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    _reconciled_game(conn)
    revisions = conn.execute("SELECT COUNT(*) FROM boxscore_revisions").fetchone()[0]

    def broken(db: sqlite3.Connection, game_id: int, now: object) -> None:
        db.execute(
            "INSERT INTO event_status (game_id, pbp_obs, shift_obs, status, checked_at, notes)"
            " VALUES (?, 1, 2, 'reconciled', 'x', '[]')",
            (GAME + 1,),
        )
        raise failure

    monkeypatch.setattr(refresh_context, "reconcile_game", broken)
    body = json.loads(fixture("nhl/boxscore_2025020709.json"))
    for side in body["playerByGameStats"].values():
        for group in side.values():
            for entry in group:
                if entry.get("goals"):
                    entry["goals"] += 1
    later = RefreshRun(conn, FakeClient({BOX: json.dumps(body).encode()}), NOW + timedelta(days=21))
    read_boxscores(later, [GAME])
    assert later.errors == []
    assert conn.execute("SELECT COUNT(*) FROM boxscore_revisions").fetchone()[0] > revisions
    assert _status(conn) == []
    assert _status(conn, GAME + 1) == []
    assert conn.execute("SELECT COUNT(*) FROM stints").fetchone() == (0,)
    assert sum(type(failure).__name__ in note for note in later.notes) == 1
