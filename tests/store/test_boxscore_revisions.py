"""Tests for versioned boxscores: revisions, the three read policies, and the legacy step (#82)."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from refresh_support import BOX_URL, MAIN_GAME, FakeClient
from seeding import add_game, drop_revision_tables

from hockey_model.sources.errors import SourceError
from hockey_model.sources.nhl import BOXSCORE_PARSER, BoxscoreRows, parse_boxscore
from hockey_model.store import boxscore_rebuild
from hockey_model.store.archive import OK, Fetch, record_observation
from hockey_model.store.boxscore_revisions import (
    FIRST,
    LATEST,
    AsOf,
    boxscore_rows,
    record_revision,
)
from hockey_model.store.db import SCHEMA_VERSION, StoreError, connect
from hockey_model.store.rebuild import rebuild_source
from hockey_model.store.refresh import refresh
from hockey_model.store.source_reads import BOXSCORES
from hockey_model.store.upgrade import upgrade_store

DAY_ONE = datetime(2026, 1, 10, 21, 0, tzinfo=UTC)
DAY_FOUR = datetime(2026, 1, 14, 9, 0, tzinfo=UTC)
GAME_START = "2026-01-10T18:00:00+00:00"
URL = f"{BOX_URL}{MAIN_GAME}/boxscore"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _boxscore(client: FakeClient | None = None) -> dict:
    return json.loads((client or FakeClient()).get("nhl", URL))


def _corrected(extra_goals: int) -> bytes:
    """The main boxscore after a correction: the first forward has more goals."""
    payload = _boxscore()
    forward = payload["playerByGameStats"]["homeTeam"]["forwards"][0]
    forward["goals"] += extra_goals
    return json.dumps(payload).encode()


def _skater_goals(conn: sqlite3.Connection, revision: int) -> list[int]:
    cursor = conn.execute(
        "SELECT goals FROM skater_game_revisions WHERE game_id = ? AND revision = ?"
        " ORDER BY nhl_id",
        (MAIN_GAME, revision),
    )
    return [goals for (goals,) in cursor]


def test_the_first_read_of_a_final_game_is_revision_one_of_its_observation(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    row = conn.execute(
        "SELECT revision, obs_id, parser, completeness, identity, fetched_at"
        " FROM boxscore_revisions WHERE game_id = ?",
        (MAIN_GAME,),
    ).fetchone()
    assert row[0] == 1
    assert row[2:5] == (BOXSCORE_PARSER, "complete", "matched")
    obs = conn.execute(
        "SELECT source, key, status, sha256 FROM observations WHERE obs_id = ?", (row[1],)
    ).fetchone()
    assert obs[:3] == (BOXSCORES, str(MAIN_GAME), "ok")
    assert obs[3] is not None
    assert row[5].startswith("2026-01-10T21:00:00")


def test_a_corrected_boxscore_adds_revision_two_and_keeps_revision_one(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    before = _skater_goals(conn, 1)
    client = FakeClient({URL: _corrected(extra_goals=1)})
    refresh(conn, client, now=DAY_FOUR)
    assert _skater_goals(conn, 1) == before
    assert sum(_skater_goals(conn, 2)) == sum(before) + 1
    assert URL in client.urls


def test_an_unchanged_revisit_adds_an_observation_and_no_revision(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    refresh(conn, FakeClient(), now=DAY_FOUR)
    assert conn.execute(
        "SELECT COUNT(*) FROM observations WHERE source = ? AND key = ?",
        (BOXSCORES, str(MAIN_GAME)),
    ).fetchone() == (2,)
    assert conn.execute(
        "SELECT COUNT(*) FROM boxscore_revisions WHERE game_id = ?", (MAIN_GAME,)
    ).fetchone() == (1,)


def test_the_three_policies_pick_one_revision_and_never_mix_two(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    refresh(conn, FakeClient({URL: _corrected(extra_goals=2)}), now=DAY_FOUR)
    first = boxscore_rows(conn, MAIN_GAME, policy=FIRST)
    latest = boxscore_rows(conn, MAIN_GAME, policy=LATEST)
    between = boxscore_rows(conn, MAIN_GAME, policy=AsOf(DAY_ONE + timedelta(days=1)))
    after = boxscore_rows(conn, MAIN_GAME, policy=AsOf(DAY_FOUR))
    assert first is not None
    assert latest is not None
    assert between is not None
    assert after is not None
    assert (first.revision, latest.revision) == (1, 2)
    assert between.revision == 1
    assert after.revision == 2
    assert sum(s.goals for s in latest.skaters) == sum(s.goals for s in first.skaters) + 2
    assert latest.skaters != first.skaters
    assert {s.team for s in latest.skaters} == {s.team for s in first.skaters}


def test_a_cutoff_before_the_first_read_gives_no_revision(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    assert boxscore_rows(conn, MAIN_GAME, policy=AsOf(DAY_ONE - timedelta(seconds=1))) is None
    assert boxscore_rows(conn, 1234567890, policy=LATEST) is None


def test_the_first_final_tables_stay_as_first_written(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    first = conn.execute(
        "SELECT SUM(goals) FROM skater_games WHERE game_id = ?", (MAIN_GAME,)
    ).fetchone()
    refresh(conn, FakeClient({URL: _corrected(extra_goals=3)}), now=DAY_FOUR)
    assert (
        conn.execute(
            "SELECT SUM(goals) FROM skater_games WHERE game_id = ?", (MAIN_GAME,)
        ).fetchone()
        == first
    )


def test_a_failed_revisit_keeps_the_old_revision_and_records_the_failure(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    report = refresh(conn, FakeClient({URL: SourceError("nhl", URL, "HTTP 503")}), now=DAY_FOUR)
    assert report.errors
    latest = boxscore_rows(conn, MAIN_GAME, policy=LATEST)
    assert latest is not None
    assert latest.revision == 1
    assert conn.execute(
        "SELECT status FROM observations WHERE source = ? AND key = ? ORDER BY obs_id DESC LIMIT 1",
        (BOXSCORES, str(MAIN_GAME)),
    ).fetchone() == ("http_error",)


def _rows(payload: bytes) -> BoxscoreRows:
    return parse_boxscore(json.loads(payload))


def test_a_rejected_boxscore_writes_no_revision_and_keeps_its_bytes(
    conn: sqlite3.Connection,
) -> None:
    body = json.loads(FakeClient().get("nhl", URL))
    body["gameState"] = "LIVE"
    refresh(conn, FakeClient({URL: json.dumps(body).encode()}), now=DAY_ONE)
    assert conn.execute(
        "SELECT COUNT(*) FROM boxscore_revisions WHERE game_id = ?", (MAIN_GAME,)
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT status, sha256 IS NOT NULL FROM observations WHERE source = ? AND key = ?",
        (BOXSCORES, str(MAIN_GAME)),
    ).fetchone() == ("shape_error", 1)


def test_record_revision_compares_rows_and_not_row_order(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    rows = _rows(FakeClient().get("nhl", URL))
    shuffled = replace(rows, skaters=list(reversed(rows.skaters)))
    obs_id = record_observation(
        conn, Fetch(BOXSCORES, URL, DAY_FOUR, OK, "nhl_boxscore:1", key=str(MAIN_GAME))
    )
    assert record_revision(conn, MAIN_GAME, shuffled, obs_id) is None


def _v9_store(path: Path, extra: Callable[[sqlite3.Connection], None] | None = None) -> Path:
    """A version 9 store: a game with both teams' rows, and a game with the home team only."""
    store = connect(path)
    add_game(store, 11, "2026-01-05T00:00:00+00:00")
    add_game(store, 12, "2026-01-06T00:00:00+00:00")
    for game_id, team, stamp in (
        (11, "TOR", "2026-01-05T04:00:00+00:00"),
        (11, "BOS", "2026-01-05T06:00:00+00:00"),
        (12, "TOR", "2026-01-06T04:00:00+00:00"),
    ):
        store.execute(
            "INSERT INTO skater_games VALUES (?,?,?,?,?,?,?)",
            (game_id, team, game_id * 100 + ord(team[0]), 1000, 1, 0, stamp),
        )
        store.execute(
            "INSERT INTO goalie_games VALUES (?,?,?,?,?,?)",
            (game_id, team, game_id * 1000 + ord(team[0]), 3600, 1, stamp),
        )
    if extra is not None:
        extra(store)
    drop_revision_tables(store)
    store.execute("UPDATE schema_version SET version = 9")
    store.commit()
    store.close()
    return path


def test_the_upgrade_writes_revision_one_for_each_stored_boxscore(tmp_path: Path) -> None:
    path = _v9_store(tmp_path / "v9.db")
    report = upgrade_store(path)
    assert (report.from_version, report.to_version) == (9, SCHEMA_VERSION)
    store = connect(path)
    try:
        rows = store.execute(
            "SELECT r.game_id, r.revision, r.parser, r.fetched_at, r.completeness, r.identity,"
            " o.status, o.sha256, o.parser, o.fetched_at"
            " FROM boxscore_revisions r JOIN observations o USING (obs_id) ORDER BY r.game_id"
        ).fetchall()
        assert [(r[0], r[1], r[2]) for r in rows] == [
            (11, 1, "legacy:unavailable"),
            (12, 1, "legacy:unavailable"),
        ]
        # The time is the newest fetch of the rows, and the bytes are marked unavailable.
        assert rows[0][3] == rows[0][9] == "2026-01-05T06:00:00+00:00"
        assert rows[0][6:9] == ("ok", None, "legacy:unavailable")
        assert [r[4:6] for r in rows] == [("complete", "matched"), ("incomplete", "matched")]
        assert store.execute("SELECT COUNT(*) FROM skater_games").fetchone() == (3,)
        assert store.execute("SELECT COUNT(*) FROM skater_game_revisions").fetchone() == (3,)
        assert store.execute("SELECT COUNT(*) FROM goalie_game_revisions").fetchone() == (3,)
        assert store.execute("SELECT COUNT(*) FROM payloads").fetchone() == (0,)
    finally:
        store.close()


def test_a_second_upgrade_run_changes_nothing(tmp_path: Path) -> None:
    path = _v9_store(tmp_path / "v9.db")
    upgrade_store(path)
    again = upgrade_store(path)
    assert again.changed is False
    store = connect(path)
    try:
        assert store.execute("SELECT COUNT(*) FROM boxscore_revisions").fetchone() == (2,)
    finally:
        store.close()


def test_the_policies_read_a_legacy_revision_by_its_stored_fetch_time(tmp_path: Path) -> None:
    path = _v9_store(tmp_path / "v9.db")
    upgrade_store(path)
    store = connect(path)
    try:
        cutoff = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)
        assert boxscore_rows(store, 11, policy=AsOf(cutoff)) is None
        legacy = boxscore_rows(store, 11, policy=LATEST)
        assert legacy is not None
        assert (legacy.revision, legacy.parser) == (1, "legacy:unavailable")
        assert legacy.fetched_at == datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
        assert {s.team for s in legacy.skaters} == {"TOR", "BOS"}
    finally:
        store.close()


def test_a_game_already_in_the_legacy_revision_adds_no_revision_for_equal_rows(
    tmp_path: Path,
) -> None:
    """An equal re-read of a legacy game is a new observation only."""
    path = _v9_store(tmp_path / "v9.db")
    upgrade_store(path)
    store = connect(path)
    try:
        legacy = boxscore_rows(store, 11, policy=FIRST)
        assert legacy is not None
        rows = BoxscoreRows(11, "TOR", "BOS", [], list(legacy.skaters), list(legacy.goalies))
        obs_id = record_observation(
            store,
            Fetch(BOXSCORES, "u", datetime(2026, 1, 20, tzinfo=UTC), OK, "nhl_boxscore:1", "11"),
        )
        assert record_revision(store, 11, rows, obs_id) is None
    finally:
        store.close()


def test_a_revisit_with_a_repeated_player_is_a_shape_error_and_adds_no_revision(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    payload = _boxscore()
    forwards = payload["playerByGameStats"]["homeTeam"]["forwards"]
    forwards.append(dict(forwards[0]))
    client = FakeClient({URL: json.dumps(payload).encode()})
    report = refresh(conn, client, now=DAY_FOUR)
    assert report.errors
    assert conn.execute(
        "SELECT COUNT(*) FROM boxscore_revisions WHERE game_id = ?", (MAIN_GAME,)
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT status FROM observations WHERE source = ? AND key = ? ORDER BY obs_id DESC LIMIT 1",
        (BOXSCORES, str(MAIN_GAME)),
    ).fetchone() == ("shape_error",)


def test_the_upgrade_marks_a_stray_team_mismatched_and_a_missing_game_unverified(
    tmp_path: Path,
) -> None:
    def extra(store: sqlite3.Connection) -> None:
        add_game(store, 13, "2026-01-07T00:00:00+00:00")
        for game_id, team in ((13, "TOR"), (13, "BOS"), (13, "MTL"), (14, "TOR"), (14, "BOS")):
            store.execute(
                "INSERT INTO skater_games VALUES (?,?,?,?,?,?,?)",
                (game_id, team, game_id * 100 + ord(team[0]) + ord(team[1]), 1000, 0, 0, "z"),
            )
            store.execute(
                "INSERT INTO goalie_games VALUES (?,?,?,?,?,?)",
                (game_id, team, game_id * 1000 + ord(team[0]) + ord(team[1]), 3600, 1, "z"),
            )

    path = _v9_store(tmp_path / "v9.db", extra)
    upgrade_store(path)
    store = connect(path)
    try:
        found = dict(
            store.execute(
                "SELECT game_id, completeness || '/' || identity FROM boxscore_revisions"
                " WHERE game_id IN (13, 14)"
            )
        )
        assert found == {13: "complete/mismatched", 14: "incomplete/unverified"}
    finally:
        store.close()


def test_a_policy_that_is_not_one_of_the_three_is_refused(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    with pytest.raises(StoreError, match="not FIRST, LATEST, or AsOf"):
        boxscore_rows(conn, MAIN_GAME, policy=DAY_ONE)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("derivation", [0, 2, -1])
def test_a_pinned_derivation_that_the_game_lacks_is_refused(
    conn: sqlite3.Connection, derivation: int
) -> None:
    refresh(conn, FakeClient(), now=DAY_ONE)
    with pytest.raises(StoreError, match=f"no derivation {derivation}"):
        boxscore_rows(conn, MAIN_GAME, policy=LATEST, derivation=derivation)
    with pytest.raises(StoreError, match="no derivation"):
        boxscore_rows(conn, 1234567890, policy=LATEST, derivation=1)


def test_a_game_read_since_the_archive_began_points_at_its_own_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vince's reproduction: the upgrade must not add a second observation of the same read."""
    path = tmp_path / "v9.db"
    store = connect(path)
    refresh(store, FakeClient(), now=DAY_ONE)
    drop_revision_tables(store)
    store.execute("UPDATE schema_version SET version = 9")
    store.commit()
    store.close()
    upgrade_store(path)
    store = connect(path)
    try:
        observations = store.execute(
            "SELECT obs_id, parser, sha256 IS NOT NULL FROM observations WHERE key = ?",
            (str(MAIN_GAME),),
        ).fetchall()
        assert observations == [(observations[0][0], BOXSCORE_PARSER, 1)]
        first = boxscore_rows(store, MAIN_GAME, policy=FIRST)
        assert first is not None
        assert (first.obs_id, first.parser) == (observations[0][0], BOXSCORE_PARSER)
        monkeypatch.setattr(boxscore_rebuild, "BOXSCORE_PARSER", "nhl_boxscore:99")
        monkeypatch.setattr(
            boxscore_rebuild,
            "parse_boxscore",
            lambda payload: _without_assists(parse_boxscore(payload)),
        )
        rebuild_source(store, BOXSCORES, "nhl_boxscore:99")
        rebuilt = boxscore_rows(store, MAIN_GAME, policy=FIRST)
        latest = boxscore_rows(store, MAIN_GAME, policy=LATEST)
        assert rebuilt is not None
        assert latest is not None
        assert (rebuilt.derivation, rebuilt.revision, rebuilt.parser) == (2, 1, "nhl_boxscore:99")
        assert latest.revision == 1
        assert all(s.assists == 0 for s in rebuilt.skaters)
    finally:
        store.close()


def _without_assists(rows: BoxscoreRows) -> BoxscoreRows:
    skaters = [s._replace(assists=0) for s in rows.skaters]
    return BoxscoreRows(rows.game_id, rows.home, rows.away, rows.players, skaters, rows.goalies)


def test_a_game_with_rows_from_two_reads_keeps_a_legacy_observation(tmp_path: Path) -> None:
    """Two fetch times mean that no single archived read gave the rows."""
    path = _v9_store(tmp_path / "v9.db")
    upgrade_store(path)
    store = connect(path)
    try:
        parser = store.execute(
            "SELECT o.parser FROM boxscore_revisions r JOIN observations o USING (obs_id)"
            " WHERE r.game_id = 11"
        ).fetchone()
        assert parser == ("legacy:unavailable",)
    finally:
        store.close()
