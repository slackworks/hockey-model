"""The 7->8 step drops the unblocked counts of mp_team_games and archives every value (#92)."""

import sqlite3
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from seeding import FETCHED, restore_unblocked_columns
from test_upgrade import make_v1_store, rows, version_of

from hockey_model.sources.moneypuck import TeamGameRow
from hockey_model.store import upgrade
from hockey_model.store.db import SCHEMA_VERSION, StoreError, connect
from hockey_model.store.moneypuck_writes import write_team_games
from hockey_model.store.shape import Requirement, earned_version
from hockey_model.store.upgrade import upgrade_store

# game_id, team, game_date, xg_for, xg_against, shots for, shots against, unblocked for,
# unblocked against, attempts_known, fetched_at
ROWS = [
    (1, "TOR", "2026-01-01", 2.0, 1.0, 50, 40, 30, 20, 1, "2026-01-02T00:00:00Z"),
    (1, "TOR", "2026-01-01", 2.1, 1.0, 51, 40, 31, 20, 1, "2026-01-09T00:00:00Z"),
    (2, "BOS", "2026-01-03", 1.0, 3.0, 45, 55, 33, 41, 1, "2026-01-04T00:00:00Z"),
    (3, "MTL", "2026-01-05", 1.5, 1.5, None, None, None, None, 0, "2026-01-06T00:00:00Z"),
]
_KEPT = (
    "game_id, team, game_date, xg_for, xg_against, shot_attempts_for, shot_attempts_against,"
    " attempts_known, fetched_at"
)


def _v7(path: Path, data: list[tuple]) -> Path:
    """Build a store that holds version 7: the current shape with the two unblocked counts."""
    connect(path).close()
    conn = sqlite3.connect(path)
    restore_unblocked_columns(conn)
    conn.executemany(
        "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against,"
        " shot_attempts_for, shot_attempts_against, unblocked_for, unblocked_against,"
        " attempts_known, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        data,
    )
    conn.execute("UPDATE schema_version SET version = 7")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def v7(tmp_path: Path) -> Path:
    return _v7(tmp_path / "v7.db", ROWS)


def _columns(path: Path) -> list[str]:
    return [row[0] for row in rows(path, "SELECT name FROM pragma_table_info('mp_team_games')")]


def test_a_store_with_the_columns_earns_version_7(v7: Path) -> None:
    conn = sqlite3.connect(v7)
    try:
        earned, missing = earned_version(conn)
    finally:
        conn.close()
    assert (earned, missing) == (
        7,
        Requirement(8, "mp_team_games", "unblocked_for", False, present=False),
    )


def test_the_step_drops_both_columns_and_keeps_every_other_value(v7: Path) -> None:
    report = upgrade_store(v7)
    assert (report.from_version, report.to_version, report.changed) == (7, SCHEMA_VERSION, True)
    assert version_of(v7) == SCHEMA_VERSION == 8
    assert "unblocked_for" not in _columns(v7)
    assert "unblocked_against" not in _columns(v7)
    kept = rows(v7, f"SELECT {_KEPT} FROM mp_team_games ORDER BY game_id, fetched_at")
    expected = [(*r[:7], r[9], r[10]) for r in ROWS]
    assert kept == expected


def test_every_stored_unblocked_value_is_archived_with_the_key_of_its_row(v7: Path) -> None:
    upgrade_store(v7)
    archive = rows(
        v7,
        "SELECT game_id, team, fetched_at, unblocked_for, unblocked_against FROM"
        " retired_mp_unblocked ORDER BY game_id, fetched_at",
    )
    assert archive == [
        (1, "TOR", "2026-01-02T00:00:00Z", 30, 20),
        (1, "TOR", "2026-01-09T00:00:00Z", 31, 20),
        (2, "BOS", "2026-01-04T00:00:00Z", 33, 41),
    ]


def test_a_row_with_no_counts_leaves_no_archive_row(tmp_path: Path) -> None:
    path = _v7(tmp_path / "unknown.db", [ROWS[3]])
    upgrade_store(path)
    assert rows(path, "SELECT COUNT(*) FROM retired_mp_unblocked") == [(0,)]


def test_the_rebuilt_table_keeps_its_index_and_its_checks(v7: Path) -> None:
    upgrade_store(v7)
    names = rows(
        v7, "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'mp_team_games'"
    )
    assert ("mp_team_games_team",) in names
    conn = sqlite3.connect(v7)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against,"
                " attempts_known, fetched_at) VALUES (9, 'X', '2026-01-01', 1, 1, 1, 'z')"
            )
    finally:
        conn.close()


def test_a_second_upgrade_changes_nothing(v7: Path) -> None:
    upgrade_store(v7)
    before = rows(v7, "SELECT * FROM retired_mp_unblocked ORDER BY rowid")
    report = upgrade_store(v7)
    assert report.changed is False
    assert rows(v7, "SELECT * FROM retired_mp_unblocked ORDER BY rowid") == before


def test_a_failed_step_rolls_everything_back(v7: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = (*upgrade.MIGRATIONS[7][:-1], "INSERT INTO no_such_table VALUES (1)")
    monkeypatch.setitem(upgrade.MIGRATIONS, 7, broken)
    with pytest.raises(StoreError, match="the store is unchanged"):
        upgrade_store(v7)
    assert version_of(v7) == 7
    assert "unblocked_for" in _columns(v7)
    assert rows(v7, "SELECT COUNT(*) FROM mp_team_games") == [(len(ROWS),)]
    tables = rows(v7, "SELECT name FROM sqlite_master WHERE type = 'table'")
    assert ("retired_mp_unblocked",) not in tables
    assert ("mp_team_games_v8",) not in tables


def test_a_missing_table_does_not_hold_the_dropped_column_shape(tmp_path: Path) -> None:
    path = tmp_path / "bare.db"
    connect(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP VIEW latest_mp_team_games")
    conn.execute("DROP TABLE mp_team_games")
    conn.commit()
    assert earned_version(conn)[0] < SCHEMA_VERSION
    conn.close()


def test_the_next_read_of_the_file_writes_no_row_again(v7: Path) -> None:
    """The stored values equal the parsed values on the kept columns, so no history is copied."""
    upgrade_store(v7)
    conn = connect(v7)
    try:
        again = [
            TeamGameRow(1, "TOR", "2026-01-01", 2.1, 1.0, 51, 40),
            TeamGameRow(2, "BOS", "2026-01-03", 1.0, 3.0, 45, 55),
        ]
        assert write_team_games(conn, again, FETCHED) == 0
    finally:
        conn.close()


def test_a_version_1_store_reaches_the_new_shape(tmp_path: Path) -> None:
    path = make_v1_store(tmp_path / "v1.db")
    upgrade_store(path)
    assert version_of(path) == SCHEMA_VERSION
    assert "unblocked_for" not in _columns(path)
    assert rows(path, "SELECT COUNT(*) FROM retired_mp_unblocked") == [(0,)]


_COUNT = st.one_of(st.none(), st.integers(min_value=0, max_value=90))


@settings(max_examples=25, deadline=None)
@given(counts=st.lists(st.tuples(_COUNT, _COUNT), min_size=1, max_size=6))
def test_any_unblocked_value_is_archived_and_every_kept_value_survives(
    tmp_path_factory: pytest.TempPathFactory, counts: list[tuple[int | None, int | None]]
) -> None:
    data = []
    for number, (unblocked_for, unblocked_against) in enumerate(counts):
        known = unblocked_for is not None and unblocked_against is not None
        shots = (50, 40) if known else (None, None)
        data.append(
            (
                number,
                "TOR",
                "2026-01-01",
                1.0,
                2.0,
                *shots,
                unblocked_for if known else None,
                unblocked_against if known else None,
                1 if known else 0,
                f"2026-01-02T00:{number:02d}:00Z",
            )
        )
    path = _v7(tmp_path_factory.mktemp("v8") / "store.db", data)
    upgrade_store(path)
    kept = rows(path, f"SELECT {_KEPT} FROM mp_team_games ORDER BY game_id")
    assert kept == [(*r[:7], r[9], r[10]) for r in data]
    archive = rows(
        path,
        "SELECT game_id, unblocked_for, unblocked_against FROM retired_mp_unblocked"
        " ORDER BY game_id",
    )
    assert archive == [(r[0], r[7], r[8]) for r in data if r[7] is not None]
