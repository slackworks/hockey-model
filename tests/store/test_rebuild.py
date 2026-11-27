"""Tests for ``store rebuild``: archived boxscores parsed again, with no network (#82)."""

import argparse
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from refresh_support import BOX_URL, MAIN_GAME, FakeClient
from seeding import add_game, drop_revision_tables

from hockey_model.sources.nhl import BOXSCORE_PARSER, BoxscoreRows, parse_boxscore
from hockey_model.sources.shape_errors import SourceParseError
from hockey_model.store import boxscore_rebuild
from hockey_model.store.archive import OK, SHAPE_ERROR, Fetch, record_observation
from hockey_model.store.boxscore_revisions import FIRST, LATEST, boxscore_rows
from hockey_model.store.db import StoreError, connect
from hockey_model.store.rebuild import rebuild_source
from hockey_model.store.refresh import refresh
from hockey_model.store.source_reads import BOXSCORES
from hockey_model.store.upgrade import upgrade_store
from hockey_model.store_cli import run_store_command

DAY_ONE = datetime(2026, 1, 10, 21, 0, tzinfo=UTC)
PARSER = BOXSCORE_PARSER
NEXT_PARSER = "nhl_boxscore:99"
URL = f"{BOX_URL}{MAIN_GAME}/boxscore"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _new_parser(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[BoxscoreRows], BoxscoreRows]
) -> None:
    """Pretend that a parser release changed the parsed rows."""
    monkeypatch.setattr(boxscore_rebuild, "BOXSCORE_PARSER", NEXT_PARSER)
    monkeypatch.setattr(
        boxscore_rebuild, "parse_boxscore", lambda payload: change(parse_boxscore(payload))
    )


def _no_assists(rows: BoxscoreRows) -> BoxscoreRows:
    skaters = [s._replace(assists=0) for s in rows.skaters]
    return BoxscoreRows(rows.game_id, rows.home, rows.away, rows.players, skaters, rows.goalies)


def _observation_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


def test_a_rebuild_with_the_same_parser_changes_nothing(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    before = _observation_count(conn)
    report = rebuild_source(conn, BOXSCORES, PARSER)
    assert report.derived == 0
    assert report.unchanged == report.games > 0
    assert _observation_count(conn) == before
    assert conn.execute("SELECT MAX(derivation) FROM boxscore_revisions").fetchone() == (1,)


def test_a_new_parser_writes_a_new_derivation_and_keeps_the_old_one(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    old = boxscore_rows(conn, MAIN_GAME, policy=LATEST)
    assert old is not None
    before = _observation_count(conn)
    _new_parser(monkeypatch, _no_assists)
    report = rebuild_source(conn, BOXSCORES, NEXT_PARSER)
    assert report.derived == report.games > 0
    assert _observation_count(conn) == before
    new = boxscore_rows(conn, MAIN_GAME, policy=LATEST)
    assert new is not None
    assert (new.derivation, new.parser) == (2, NEXT_PARSER)
    assert all(s.assists == 0 for s in new.skaters)
    assert new.fetched_at == old.fetched_at
    assert new.obs_id == old.obs_id
    pinned = boxscore_rows(conn, MAIN_GAME, policy=LATEST, derivation=1)
    assert pinned == old


def test_a_second_rebuild_with_the_same_new_parser_adds_no_derivation(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    _new_parser(monkeypatch, _no_assists)
    rebuild_source(conn, BOXSCORES, NEXT_PARSER)
    again = rebuild_source(conn, BOXSCORES, NEXT_PARSER)
    assert again.derived == 0
    assert conn.execute("SELECT MAX(derivation) FROM boxscore_revisions").fetchone() == (2,)


def test_a_rebuild_makes_no_network_request(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    _new_parser(monkeypatch, _no_assists)
    rebuild_source(conn, BOXSCORES, NEXT_PARSER)  # takes no client, so none can be called


def test_a_payload_that_the_old_parser_rejected_becomes_a_revision(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.loads(FakeClient().get("nhl", URL))
    body["gameState"] = "LIVE"
    refresh(conn, FakeClient({URL: json.dumps(body).encode()}), now=DAY_ONE)
    assert boxscore_rows(conn, MAIN_GAME, policy=LATEST) is None

    def lenient(payload: object) -> BoxscoreRows:
        assert isinstance(payload, dict)
        return parse_boxscore({**payload, "gameState": "OFF"})

    monkeypatch.setattr(boxscore_rebuild, "BOXSCORE_PARSER", NEXT_PARSER)
    monkeypatch.setattr(boxscore_rebuild, "parse_boxscore", lenient)
    rebuild_source(conn, BOXSCORES, NEXT_PARSER)
    revived = boxscore_rows(conn, MAIN_GAME, policy=FIRST)
    assert revived is not None
    assert (revived.derivation, revived.revision) == (1, 1)
    assert revived.fetched_at == DAY_ONE
    status = conn.execute(
        "SELECT status FROM observations WHERE obs_id = ?", (revived.obs_id,)
    ).fetchone()
    assert status == (SHAPE_ERROR,)


def test_a_payload_that_names_the_wrong_teams_is_refused_and_counted(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    wrong = json.loads(FakeClient().get("nhl", URL))
    wrong["homeTeam"]["abbrev"], wrong["awayTeam"]["abbrev"] = "TOR", "MTL"
    record_observation(
        conn,
        Fetch(
            BOXSCORES, URL, DAY_ONE, SHAPE_ERROR, PARSER, str(MAIN_GAME), json.dumps(wrong).encode()
        ),
    )
    _new_parser(monkeypatch, _no_assists)
    report = rebuild_source(conn, BOXSCORES, NEXT_PARSER)
    assert report.rejected == 1
    assert f"game {MAIN_GAME}" in report.refused[0]
    assert "TOR" in report.refused[0] or "stored game" in report.refused[0]
    latest = boxscore_rows(conn, MAIN_GAME, policy=LATEST)
    assert latest is not None
    assert latest.revision == 1


def test_a_legacy_revision_carries_into_the_new_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revision with no bytes stays revision 1, and later bytes add revision 2."""
    path = tmp_path / "v9.db"
    store = connect(path)
    add_game(store, MAIN_GAME, "2026-01-10T18:00:00+00:00", home="BOS", away="NYR")
    store.execute(
        "INSERT INTO skater_games VALUES (?,?,?,?,?,?,?)",
        (MAIN_GAME, "BOS", 1, 1000, 0, 0, "2026-01-10T21:00:00+00:00"),
    )
    store.execute(
        "INSERT INTO goalie_games VALUES (?,?,?,?,?,?)",
        (MAIN_GAME, "BOS", 2, 3600, 1, "2026-01-10T21:00:00+00:00"),
    )
    drop_revision_tables(store)
    store.execute("UPDATE schema_version SET version = 9")
    store.commit()
    store.close()
    upgrade_store(path)
    store = connect(path)
    try:
        payload = FakeClient().get("nhl", URL)
        later = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        record_observation(store, Fetch(BOXSCORES, URL, later, OK, PARSER, str(MAIN_GAME), payload))
        report = rebuild_source(store, BOXSCORES, PARSER)
        assert report.derived == 1
        first = boxscore_rows(store, MAIN_GAME, policy=FIRST)
        latest = boxscore_rows(store, MAIN_GAME, policy=LATEST)
        assert first is not None
        assert latest is not None
        assert (first.derivation, first.revision, first.parser) == (2, 1, "legacy:unavailable")
        assert (latest.derivation, latest.revision, latest.parser) == (2, 2, PARSER)
        assert latest.fetched_at == later
        assert boxscore_rows(store, MAIN_GAME, policy=LATEST, derivation=1) is not None
    finally:
        store.close()


def test_a_rebuild_refuses_a_stale_parser_and_an_unknown_source(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(StoreError, match=f"--parser {BOXSCORE_PARSER}"):
        rebuild_source(conn, BOXSCORES, "nhl_boxscore:0")
    with pytest.raises(StoreError, match="nhl_boxscores"):
        rebuild_source(conn, "espn_starters", PARSER)


def test_the_store_command_prints_the_report(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = connect(path)
    refresh(store, FakeClient(), now=DAY_ONE)
    store.close()
    args = argparse.Namespace(
        command="store",
        store_command="rebuild",
        db=str(path),
        source=BOXSCORES,
        parser=PARSER,
        allow_refused=False,
    )
    payload, code = run_store_command(args)
    assert code == 0
    assert isinstance(payload, dict)
    assert payload["source"] == BOXSCORES
    assert payload["derived"] == 0


def test_the_store_command_names_a_missing_store(tmp_path: Path) -> None:
    args = argparse.Namespace(
        command="store",
        store_command="rebuild",
        db=str(tmp_path / "none.db"),
        source=BOXSCORES,
        parser=PARSER,
        allow_refused=False,
    )
    with pytest.raises(StoreError, match="no store file"):
        run_store_command(args)


def test_a_payload_that_the_first_read_accepted_and_the_parser_refuses_stops_the_rebuild(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser regression must not replace a good derivation with a lossy one."""
    refresh(conn, FakeClient(), now=DAY_ONE)

    def broken(payload: object) -> BoxscoreRows:
        raise SourceParseError("nhl", "the parser lost the shape of the feed")

    monkeypatch.setattr(boxscore_rebuild, "BOXSCORE_PARSER", NEXT_PARSER)
    monkeypatch.setattr(boxscore_rebuild, "parse_boxscore", broken)
    with pytest.raises(StoreError, match=r"game \d+ observation \d+: the parser .* refuses"):
        rebuild_source(conn, BOXSCORES, NEXT_PARSER)
    assert conn.execute("SELECT MAX(derivation) FROM boxscore_revisions").fetchone() == (1,)


def test_a_deeply_nested_archived_body_is_a_refusal_and_not_a_crash(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    record_observation(
        conn,
        Fetch(BOXSCORES, URL, DAY_ONE, SHAPE_ERROR, PARSER, str(MAIN_GAME), b"[" * 200_000),
    )
    report = rebuild_source(conn, BOXSCORES, PARSER)
    assert report.rejected == 1
    assert "not valid JSON" in report.refused[0]
    assert report.derived == 0


def test_a_damaged_payload_row_names_the_game_and_the_observation(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    conn.execute("UPDATE payloads SET body = x'00'")
    conn.commit()
    with pytest.raises(StoreError, match=rf"rebuild {BOXSCORES} game \d+ observation \d+"):
        rebuild_source(conn, BOXSCORES, PARSER)


def _duplicate_player_payload() -> bytes:
    body = json.loads(FakeClient().get("nhl", URL))
    forwards = body["playerByGameStats"]["homeTeam"]["forwards"]
    forwards.append(dict(forwards[0]))
    return json.dumps(body).encode()


def test_a_payload_that_an_older_parser_accepted_needs_allow_refused_to_leave(
    conn: sqlite3.Connection,
) -> None:
    """The stricter parser (#82) refuses a repeated player that version 1 stored as ok."""
    refresh(conn, FakeClient(), now=DAY_ONE)
    record_observation(
        conn,
        Fetch(
            BOXSCORES,
            URL,
            DAY_ONE,
            OK,
            "nhl_boxscore:1",
            str(MAIN_GAME),
            _duplicate_player_payload(),
        ),
    )
    with pytest.raises(StoreError, match="--allow-refused"):
        rebuild_source(conn, BOXSCORES, PARSER)
    assert conn.execute("SELECT MAX(derivation) FROM boxscore_revisions").fetchone() == (1,)


def test_allow_refused_drops_the_payload_and_names_it(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    record_observation(
        conn,
        Fetch(
            BOXSCORES,
            URL,
            DAY_ONE,
            OK,
            "nhl_boxscore:1",
            str(MAIN_GAME),
            _duplicate_player_payload(),
        ),
    )
    report = rebuild_source(conn, BOXSCORES, PARSER, allow_refused=True)
    assert report.rejected == 1
    assert "twice" in report.refused[0]
    latest = boxscore_rows(conn, MAIN_GAME, policy=LATEST)
    assert latest is not None
    assert latest.revision == 1
