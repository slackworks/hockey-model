"""Tests for the store upgrade: a version 1 store moves to the current version, keeping rows."""

import argparse
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from seeding import add_game, add_retired_columns, add_team_form, stamp_old_version

from hockey_model.store import migrations, sql_text, upgrade
from hockey_model.store.db import SCHEMA_VERSION, StoreError, connect
from hockey_model.store.reads import mp_attempt_form_before
from hockey_model.store.shape import Requirement, earned_version
from hockey_model.store.upgrade import upgrade_store
from hockey_model.store_cli import run_store_command

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "store"
V1_SCHEMA = FIXTURES / "schema_v1.sql"
V1_MP_ROWS = [
    (2001, "TOR", "2026-01-01", 1.5, 2.5, "2026-01-02T00:00:00Z"),
    (2001, "TOR", "2026-01-01", 1.6, 2.4, "2026-01-03T00:00:00Z"),
    (2002, "BOS", "2026-01-05", 3.1, 0.9, "2026-01-06T00:00:00Z"),
]
MP_KEPT_COLUMNS = "game_id, team, game_date, xg_for, xg_against, fetched_at"


def make_v1_store(path: Path, mp_rows: list[tuple] = V1_MP_ROWS) -> Path:
    """Write a version 1 store with the given MoneyPuck rows and one saved fit."""
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.executemany(
        "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        mp_rows,
    )
    conn.execute(
        "INSERT INTO model_fits (fit_id, fit_through_season, features, weights, created_at)"
        " VALUES (7, 20252026, '[\"rest\"]', '[0.1]', '2026-11-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    return path


def make_v2_store(path: Path) -> Path:
    """Write a version 2 store: a fresh store with the fit label table removed."""
    connect(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE fit_labels")
    stamp_old_version(conn, 2)
    conn.execute(
        "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
        " VALUES (20252026, '[\"rest\"]', '[0.1]', '2026-11-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    return path


def make_v2_store_preflag(path: Path) -> Path:
    """Write a version 2 store with the table shape that commit 62cc6fb replaced.

    The four count columns are there and ``attempts_known`` is not. That is the shape the
    code wrote before 62cc6fb, and it kept the version at 2.
    """
    connect(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE fit_labels")
    conn.execute("DROP TABLE mp_team_games")
    conn.execute(
        "CREATE TABLE mp_team_games ("
        " game_id INTEGER, team TEXT, game_date TEXT, xg_for REAL, xg_against REAL,"
        " shot_attempts_for INTEGER, shot_attempts_against INTEGER,"
        " unblocked_for INTEGER, unblocked_against INTEGER, fetched_at TEXT NOT NULL,"
        " PRIMARY KEY (game_id, team, fetched_at))"
    )
    conn.execute(
        "INSERT INTO mp_team_games VALUES (2002, 'BOS', '2026-01-05', 3.1, 0.9,"
        " 10, 11, 12, 13, '2026-01-06T00:00:00Z')"
    )
    stamp_old_version(conn, 2)
    conn.execute(
        "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
        " VALUES (20252026, '[\"rest\"]', '[0.1]', '2026-11-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    return path


def make_v3_store(path: Path) -> Path:
    """Write a version 3 store: the shape that the store cache holds today.

    The flag is there, and one row carries a count set while the flag says unknown. The
    3->4 step must keep that row's flag and derive the other one.
    """
    connect(path).close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against,"
        " shot_attempts_for, shot_attempts_against, unblocked_for, unblocked_against,"
        " fetched_at, attempts_known) VALUES (3001, 'NYR', '2026-01-09', 2.0, 1.0,"
        " 20, 21, 22, 23, '2026-01-10T00:00:00Z', 1)"
    )
    conn.execute(
        "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against,"
        " fetched_at, attempts_known) VALUES (3002, 'NYR', '2026-01-11', 1.0, 1.0,"
        " '2026-01-12T00:00:00Z', 0)"
    )
    stamp_old_version(conn, 3)
    conn.commit()
    conn.close()
    return path


def rows(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def version_of(path: Path) -> int:
    return rows(path, "SELECT version FROM schema_version")[0][0]


def _columns(path: Path) -> list[tuple]:
    """Return name, type, not-null, default, and key position of each mp_team_games column."""
    return rows(
        path,
        "SELECT name, type, \"notnull\", dflt_value, pk FROM pragma_table_info('mp_team_games')"
        " ORDER BY cid",
    )


@pytest.fixture
def v1(tmp_path: Path) -> Path:
    return make_v1_store(tmp_path / "store.db")


def test_upgrade_keeps_every_row_and_leaves_attempt_counts_unknown(v1: Path) -> None:
    before = rows(v1, f"SELECT {MP_KEPT_COLUMNS} FROM mp_team_games ORDER BY game_id, fetched_at")
    report = upgrade_store(v1)
    assert (report.from_version, report.to_version, report.changed) == (1, SCHEMA_VERSION, True)
    after = rows(v1, f"SELECT {MP_KEPT_COLUMNS} FROM mp_team_games ORDER BY game_id, fetched_at")
    assert after == before
    counts = rows(
        v1,
        "SELECT shot_attempts_for, shot_attempts_against, unblocked_for, unblocked_against,"
        " attempts_known FROM mp_team_games",
    )
    assert counts == [(None, None, None, None, 0)] * len(V1_MP_ROWS)
    assert version_of(v1) == SCHEMA_VERSION


def test_upgrade_keeps_fit_ids_and_records_the_run(v1: Path) -> None:
    fit_before = rows(v1, "SELECT * FROM model_fits")
    report = upgrade_store(v1)
    assert rows(v1, "SELECT * FROM model_fits") == fit_before
    ledger = rows(v1, "SELECT from_version, to_version, backup_path FROM schema_migrations")
    assert ledger == [(1, SCHEMA_VERSION, report.backup_path)]


def test_upgrade_writes_a_backup_that_still_reads_as_version_1(v1: Path) -> None:
    report = upgrade_store(v1)
    assert report.backup_path is not None
    backup = Path(report.backup_path)
    assert backup.name == "store.db.v1.bak"
    assert version_of(backup) == 1
    assert len(rows(backup, "SELECT * FROM mp_team_games")) == len(V1_MP_ROWS)


def test_a_second_upgrade_is_a_no_op_that_names_the_last_backup(v1: Path) -> None:
    first = upgrade_store(v1)
    again = upgrade_store(v1)
    assert again.changed is False
    assert again.backup_path == first.backup_path
    assert len(rows(v1, "SELECT * FROM schema_migrations")) == 1


def test_a_failed_change_rolls_back_and_keeps_version_1(
    v1: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = (upgrade.MIGRATIONS[1][0], "INSERT INTO no_such_table VALUES (1)")
    monkeypatch.setitem(upgrade.MIGRATIONS, 1, broken)
    with pytest.raises(StoreError, match="the store is unchanged"):
        upgrade_store(v1)
    assert version_of(v1) == 1
    assert "attempts_known" not in [row[0] for row in _columns(v1)]
    assert rows(v1, "SELECT COUNT(*) FROM mp_team_games") == [(len(V1_MP_ROWS),)]


def test_an_existing_backup_is_never_overwritten(v1: Path) -> None:
    (v1.parent / "store.db.v1.bak").write_text("keep me", encoding="utf-8")
    with pytest.raises(StoreError, match="already exists"):
        upgrade_store(v1)
    assert (v1.parent / "store.db.v1.bak").read_text(encoding="utf-8") == "keep me"
    assert version_of(v1) == 1


def test_a_missing_file_is_refused_without_being_created(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"
    with pytest.raises(StoreError, match="no store file there"):
        upgrade_store(path)
    assert not path.exists()


def test_if_missing_skip_returns_without_creating_the_file(tmp_path: Path) -> None:
    """A first run upgrades nothing and continues to the refresh that writes the store."""
    path = tmp_path / "absent.db"
    report = upgrade_store(path, if_missing="skip")
    assert (report.from_version, report.to_version, report.changed) == (None, SCHEMA_VERSION, False)
    assert report.next_step is None
    assert report.note is not None and "first refresh" in report.note
    assert not path.exists()


def test_a_directory_is_refused_even_when_a_missing_file_is_skipped(tmp_path: Path) -> None:
    """A directory is not an absent store, so a skip would hide a wrong path."""
    with pytest.raises(StoreError, match="not a regular file"):
        upgrade_store(tmp_path, if_missing="skip")


def test_an_unreadable_directory_is_named(tmp_path: Path) -> None:
    """A parent that cannot be entered is not an absent store, so the error names it."""
    if os.geteuid() == 0:
        pytest.skip("root reads a directory that has no permission bits")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0)
    try:
        with pytest.raises(StoreError, match="cannot read the path"):
            upgrade_store(blocked / "store.db", if_missing="skip")
    finally:
        blocked.chmod(0o755)


def test_a_newer_store_is_refused(v1: Path) -> None:
    conn = sqlite3.connect(v1)
    conn.execute(f"UPDATE schema_version SET version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="no upgrade path"):
        upgrade_store(v1)


def test_the_open_path_refuses_version_1_and_names_the_upgrade(v1: Path) -> None:
    with pytest.raises(StoreError, match="hockey-model store upgrade"):
        connect(v1)
    assert version_of(v1) == 1


def test_the_store_command_runs_the_upgrade_and_prints_the_report(v1: Path) -> None:
    args = argparse.Namespace(
        command="store", store_command="upgrade", db=str(v1), if_missing="fail"
    )
    payload, code = run_store_command(args)
    assert code == 0
    assert isinstance(payload, dict)
    assert payload["to_version"] == SCHEMA_VERSION and payload["changed"] is True


def test_the_store_command_skips_a_missing_file_when_asked(tmp_path: Path) -> None:
    args = argparse.Namespace(
        command="store",
        store_command="upgrade",
        db=str(tmp_path / "absent.db"),
        if_missing="skip",
    )
    payload, code = run_store_command(args)
    assert code == 0
    assert isinstance(payload, dict)
    assert payload["from_version"] is None and payload["changed"] is False
    assert not (tmp_path / "absent.db").exists()


def test_the_store_command_expands_a_tilde_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--db follows the same path rule as HOCKEY_MODEL_DB, which expands a home shortcut."""
    monkeypatch.setenv("HOME", str(tmp_path))
    args = argparse.Namespace(
        command="store", store_command="upgrade", db="~/absent.db", if_missing="skip"
    )
    payload, code = run_store_command(args)
    assert code == 0
    assert isinstance(payload, dict)
    assert payload["path"] == str(tmp_path / "absent.db")


def test_the_store_command_needs_a_subcommand(v1: Path) -> None:
    args = argparse.Namespace(command="store", store_command=None, db=str(v1))
    with pytest.raises(StoreError, match="name a subcommand"):
        run_store_command(args)


def test_attempt_form_skips_rows_without_counts(v1: Path) -> None:
    upgrade_store(v1)
    conn = connect(v1)
    add_game(conn, 2001, "2026-01-01T23:00:00Z", "TOR", "BOS")
    add_game(conn, 2002, "2026-01-05T23:00:00Z", "TOR", "BOS")
    add_team_form(conn, 2002, "TOR", 2.0, 2.0, attempts=(55, 45))
    form = mp_attempt_form_before(conn, "TOR", datetime(2026, 2, 1, tzinfo=UTC), 5)
    conn.close()
    assert form == [(55, 45)]


def test_an_upgraded_table_has_the_same_columns_as_a_fresh_one(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    connect(fresh).close()
    upgraded = make_v1_store(tmp_path / "upgraded.db")
    upgrade_store(upgraded)
    assert _columns(upgraded) == _columns(fresh)


def test_a_fresh_row_without_counts_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    connect(path).close()
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against, fetched_at)"
            " VALUES (1, 'TOR', '2026-01-01', 1.0, 1.0, '2026-01-02T00:00:00Z')"
        )
    conn.close()


def test_views_exist_after_the_upgrade(v1: Path) -> None:
    upgrade_store(v1)
    names = {row[0] for row in rows(v1, "SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert {"latest_mp_team_games", "latest_games"} <= names


_ROW = st.tuples(
    st.integers(1, 10**9),
    st.sampled_from(["TOR", "BOS", "NYR"]),
    st.from_regex(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]", fullmatch=True),
    st.floats(0, 10, allow_nan=False, allow_infinity=False),
    st.floats(0, 10, allow_nan=False, allow_infinity=False),
    st.from_regex(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]T00:00:00Z", fullmatch=True),
)


@settings(max_examples=25, deadline=None)
@given(data=st.lists(_ROW, min_size=0, max_size=8, unique_by=lambda r: (r[0], r[1], r[5])))
def test_any_v1_rows_keep_their_values_and_gain_unknown_counts(data: list[tuple]) -> None:
    # Each example needs its own directory, because a store and its backup must not exist yet.
    with tempfile.TemporaryDirectory() as folder:
        path = make_v1_store(Path(folder) / "prop.db", mp_rows=data)
        upgrade_store(path)
        kept = rows(path, f"SELECT {MP_KEPT_COLUMNS} FROM mp_team_games")
        assert sorted(kept) == sorted(data)
        counts = rows(path, "SELECT shot_attempts_for, attempts_known FROM mp_team_games")
        assert all(count == (None, 0) for count in counts)


def test_an_unknown_attempt_column_is_refused_before_any_sql_runs(v1: Path) -> None:
    upgrade_store(v1)
    conn = connect(v1)
    try:
        with pytest.raises(ValueError, match="unknown column"):
            from hockey_model.store.reads import _mp_pairs_before

            _mp_pairs_before(
                conn,
                ("xg_for; DROP TABLE games", "xg_against"),
                "TOR",
                datetime(2026, 2, 1, tzinfo=UTC),
                5,
            )
    finally:
        conn.close()


def _index_names(path: Path) -> set[str]:
    """Return the index names of the store, without SQLite's automatic key indexes."""
    found = rows(path, "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")
    return {row[0] for row in found}


def _table_sql(path: Path) -> str:
    """Return the table's SQL with comments, quotes, and whitespace removed, for comparison."""
    text = rows(
        path, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'mp_team_games'"
    )[0][0]
    body = re.sub(r"--[^\n]*", "", text)
    body = re.sub(r"CREATE TABLE \S+", "CREATE TABLE t", body, count=1)
    return re.sub(r"\s+", " ", body.replace('"', "")).strip().lower()


def test_the_upgraded_table_sql_matches_the_fresh_one_including_checks(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    connect(fresh).close()
    upgraded = make_v1_store(tmp_path / "upgraded.db")
    upgrade_store(upgraded)
    assert _table_sql(upgraded) == _table_sql(fresh)


def test_a_completed_upgrade_says_what_to_run_next(v1: Path) -> None:
    report = upgrade_store(v1)
    assert report.next_step is not None
    assert "hockey-model refresh --reread-moneypuck" in report.next_step


def test_a_version_2_store_gains_the_fit_label_table_and_keeps_its_fit(
    tmp_path: Path,
) -> None:
    # The upgrade adds the label table but not a label. An operator sets the label, because
    # only a person knows which saved fit the daily run should serve.
    v2 = make_v2_store(tmp_path / "store.db")
    report = upgrade_store(v2)
    assert (report.from_version, report.to_version) == (2, SCHEMA_VERSION)
    assert rows(v2, "SELECT fit_id, fit_through_season FROM model_fits") == [(1, 20252026)]
    assert rows(v2, "SELECT COUNT(*) FROM fit_labels") == [(0,)]
    assert version_of(v2) == SCHEMA_VERSION
    # The 2->3 step adds the label table, and the 3->4 rebuild leaves every count alone.
    assert report.next_step is None


def test_a_version_2_store_without_the_flag_gains_it_and_keeps_its_counts(
    tmp_path: Path,
) -> None:
    """Version 2 covered two table shapes. The earlier one has no attempts_known column.

    Commit 62cc6fb added the flag to schema.sql without a version bump, so a store that
    the earlier code wrote is still version 2 and cannot answer a query that names the
    flag. The 3->4 step must add it and derive it from the counts.
    """
    v2 = make_v2_store_preflag(tmp_path / "store.db")
    report = upgrade_store(v2)
    assert (report.from_version, report.to_version) == (2, SCHEMA_VERSION)
    assert version_of(v2) == SCHEMA_VERSION
    assert "attempts_known" in [row[1] for row in rows(v2, "PRAGMA table_info(mp_team_games)")]
    assert rows(
        v2,
        "SELECT game_id, team, shot_attempts_for, unblocked_against, attempts_known"
        " FROM mp_team_games",
    ) == [(2002, "BOS", 10, 13, 1)]
    assert report.next_step is None


def test_a_version_3_store_keeps_the_flag_it_has_and_derives_the_other(
    tmp_path: Path,
) -> None:
    """Version 3 is the shape the store cache holds, so 3->4 is the transition that runs."""
    v3 = make_v3_store(tmp_path / "store.db")
    report = upgrade_store(v3)
    assert (report.from_version, report.to_version) == (3, SCHEMA_VERSION)
    assert rows(v3, "SELECT game_id, attempts_known FROM mp_team_games ORDER BY game_id") == [
        (3001, 1),
        (3002, 0),
    ]
    assert version_of(v3) == SCHEMA_VERSION


def test_a_version_3_store_gains_the_current_table_sql(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    connect(fresh).close()
    v3 = make_v3_store(tmp_path / "upgraded.db")
    upgrade_store(v3)
    assert _table_sql(v3) == _table_sql(fresh)


def test_a_version_2_store_without_the_flag_gains_the_current_table_sql(
    tmp_path: Path,
) -> None:
    """The preflag path runs the same rebuild, so it needs the same comparison."""
    fresh = tmp_path / "fresh.db"
    connect(fresh).close()
    upgraded = make_v2_store_preflag(tmp_path / "upgraded.db")
    upgrade_store(upgraded)
    assert _table_sql(upgraded) == _table_sql(fresh)


def test_an_upgraded_store_has_the_same_indexes_as_a_fresh_one(tmp_path: Path) -> None:
    """Five indexes reached schema.sql after version 1, so no early store had them.

    The v1 fixture holds the first schema, which has no index at all, so this test fails on a
    migration that never creates one.
    """
    early = (
        "games_start",
        "goalie_games_team",
        "mp_goalie_games_player",
        "starter_reads_game",
        "injury_reads_time",
    )
    fresh = tmp_path / "fresh.db"
    connect(fresh).close()
    upgraded = make_v1_store(tmp_path / "upgraded.db")
    assert _index_names(upgraded) == set()

    upgrade_store(upgraded)
    assert _index_names(upgraded) == _index_names(fresh)
    assert set(early) <= _index_names(upgraded)


def test_a_version_2_store_with_empty_attempt_counts_gets_the_reread_instruction(
    tmp_path: Path,
) -> None:
    """A row the 1->2 step wrote keeps NULL counts, so the reread is a real step to take."""
    v2 = make_v2_store(tmp_path / "store.db")
    conn = sqlite3.connect(v2)
    conn.execute(
        "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against,"
        " fetched_at, attempts_known) VALUES (2001, 'TOR', '2026-01-01', 1.5, 2.5,"
        " '2026-01-02T00:00:00Z', 0)"
    )
    conn.commit()
    conn.close()
    report = upgrade_store(v2)
    assert report.next_step is not None
    assert "hockey-model refresh --reread-moneypuck" in report.next_step


def test_a_reread_row_stops_the_instruction_even_before_the_upgrade(tmp_path: Path) -> None:
    """A store that started at v1 ran the 1->2 step, then reread MoneyPuck, then upgrades again.

    The reread appends a counted row and keeps the v1 row, so a count of the table would
    still see an empty row and ask for a reread that has already happened.
    """
    v2 = make_v2_store(tmp_path / "store.db")
    conn = sqlite3.connect(v2)
    conn.execute(
        "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against,"
        " fetched_at, attempts_known) VALUES (2001, 'TOR', '2026-01-01', 1.5, 2.5,"
        " '2026-01-02T00:00:00Z', 0)"
    )
    conn.execute(
        "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against,"
        " shot_attempts_for, shot_attempts_against, unblocked_for, unblocked_against,"
        " fetched_at, attempts_known) VALUES (2001, 'TOR', '2026-01-01', 1.5, 2.5,"
        " 1, 2, 3, 4, '2026-01-03T00:00:00Z', 1)"
    )
    conn.commit()
    conn.close()
    assert upgrade_store(v2).next_step is None


def test_a_table_name_that_is_not_an_identifier_is_refused() -> None:
    """The DDL builder interpolates its argument, so a bad name must stop before any SQL."""
    for bad in ("mp; DROP TABLE games", "a b", "", "1t", 'x"'):
        with pytest.raises(StoreError, match="is not a table name"):
            migrations.mp_team_games_ddl(bad)


def test_a_damaged_ledger_is_named_instead_of_read_as_no_backup(v1: Path) -> None:
    """A no-op report names the last backup, so a ledger read must not answer None on error."""
    upgrade_store(v1)
    conn = sqlite3.connect(v1)
    conn.execute("DROP TABLE schema_migrations")
    conn.execute("CREATE TABLE schema_migrations (wrong TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="cannot read the upgrade ledger") as info:
        upgrade_store(v1)
    assert "damaged" in str(info.value)


def test_a_store_with_no_ledger_reports_no_backup(tmp_path: Path) -> None:
    """A fresh store has the current shape and no ledger, so its upgrade is a no-op."""
    path = tmp_path / "fresh.db"
    connect(path).close()
    report = upgrade_store(path)
    assert (report.changed, report.backup_path) == (False, None)


class _BrokenRollback:
    """A connection whose transaction is open and whose rollback fails."""

    in_transaction = True

    def execute(self, *_args: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")


def test_a_failing_rollback_is_named_beside_the_cause() -> None:
    """A lost rollback is the one case where the store may be part migrated."""
    broken = cast("sqlite3.Connection", _BrokenRollback())
    outcome = upgrade._rollback(broken)
    assert outcome == (
        "the rollback also failed: disk I/O error; the store may be partly migrated."
    )


def test_a_rollback_that_runs_reports_the_store_unchanged(v1: Path) -> None:
    conn = sqlite3.connect(v1, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    assert upgrade._rollback(conn) == "the store is unchanged."
    conn.close()


def test_a_no_op_upgrade_has_no_next_step(v1: Path) -> None:
    upgrade_store(v1)
    assert upgrade_store(v1).next_step is None


def test_an_upgraded_store_holds_the_current_shape_of_the_two_rebuilt_tables(
    tmp_path: Path,
) -> None:
    """The v1 fixture carries the nullable columns and no NOT NULL, which d05eb1a later added."""
    fresh = tmp_path / "fresh.db"
    connect(fresh).close()
    upgraded = make_v1_store(tmp_path / "upgraded.db")
    upgrade_store(upgraded)
    for table in ("source_reads", "model_fits"):
        pragma = f"SELECT name, \"notnull\" FROM pragma_table_info('{table}') ORDER BY cid"
        assert rows(upgraded, pragma) == rows(fresh, pragma)
        assert ("fetched_at", 1) in rows(upgraded, pragma) or ("created_at", 1) in rows(
            upgraded, pragma
        )


def test_the_rebuild_keeps_every_source_read_and_fit_row(tmp_path: Path) -> None:
    path = make_v1_store(tmp_path / "store.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error)"
        " VALUES ('nhl', 'https://example.test/a', '2026-01-01T00:00:00Z', 1, NULL)"
    )
    conn.execute(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error)"
        " VALUES ('espn', 'https://example.test/b', '2026-01-02T00:00:00Z', 0, 'timeout')"
    )
    conn.commit()
    conn.close()
    upgrade_store(path)
    assert rows(path, "SELECT source, url, ok, error FROM source_reads ORDER BY url") == [
        ("nhl", "https://example.test/a", 1, None),
        ("espn", "https://example.test/b", 0, "timeout"),
    ]
    assert rows(path, "SELECT fit_id, features FROM model_fits") == [(7, '["rest"]')]


_READ_ROW = st.tuples(
    st.sampled_from(["nhl", "espn", "moneypuck"]),
    st.from_regex(r"https://example\.test/[a-z]{1,8}", fullmatch=True),
    st.from_regex(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]T00:00:00Z", fullmatch=True),
    st.sampled_from([0, 1]),
    st.one_of(st.none(), st.sampled_from(["timeout", "http 500", "a 'quote'"])),
)
_FIT_ROW = st.tuples(
    st.integers(1000, 10**9),
    st.integers(20152016, 20252026),
    st.sampled_from(['["rest"]', '["rest", "xg_form"]', "[]"]),
    st.sampled_from(["[0.1]", "[-0.25, 0.5]"]),
    st.from_regex(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]T00:00:00Z", fullmatch=True),
)


@settings(max_examples=25, deadline=None)
@given(
    reads=st.lists(_READ_ROW, max_size=6),
    fits=st.lists(_FIT_ROW, max_size=4, unique_by=lambda row: row[0]),
)
def test_any_source_read_and_fit_row_survives_the_rebuild(
    reads: list[tuple], fits: list[tuple]
) -> None:
    """The 4->5 step copies both tables, so a value that it drops is a row the store loses."""
    with tempfile.TemporaryDirectory() as folder:
        path = make_v1_store(Path(folder) / "prop.db")
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM model_fits")
        conn.executemany(
            "INSERT INTO source_reads (source, url, fetched_at, ok, error) VALUES (?, ?, ?, ?, ?)",
            reads,
        )
        conn.executemany(
            "INSERT INTO model_fits (fit_id, fit_through_season, features, weights, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            fits,
        )
        conn.commit()
        conn.close()
        upgrade_store(path)
        kept = rows(path, "SELECT source, url, fetched_at, ok, error FROM source_reads")
        # A row may hold None for error, and sorting a column that mixes None with text
        # raises TypeError. Compare as multisets so the row order does not matter (#155).
        assert sorted(kept, key=repr) == sorted(reads, key=repr)
        assert sorted(rows(path, "SELECT * FROM model_fits"), key=repr) == sorted(fits, key=repr)


def test_a_null_in_a_rebuilt_column_stops_the_upgrade_and_names_the_table(
    tmp_path: Path,
) -> None:
    """The rebuild adds NOT NULL, so a stored NULL has no value it could copy into the shape."""
    path = make_v1_store(tmp_path / "store.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error)"
        " VALUES ('nhl', 'https://example.test/a', NULL, 1, NULL)"
    )
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match=r"source_reads holds 1 rows with a NULL fetched_at"):
        upgrade_store(path)
    assert version_of(path) == 1


def test_a_version_that_adds_two_columns_is_earned_only_when_both_hold(tmp_path: Path) -> None:
    """Version 5 adds two columns. A later one that fails must give version 4, not version 5."""
    path = tmp_path / "store.db"
    connect(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE model_fits")
    conn.execute(
        "CREATE TABLE model_fits (fit_id INTEGER PRIMARY KEY, fit_through_season INTEGER,"
        " features TEXT, weights TEXT, created_at TEXT)"
    )
    conn.commit()
    try:
        earned, missing = earned_version(conn)
    finally:
        conn.close()
    assert (earned, missing) == (4, Requirement(5, "model_fits", "created_at", True))


def test_a_step_that_cannot_repair_the_table_shape_stops_the_upgrade(tmp_path: Path) -> None:
    """The 2->3 step only creates the table, so it leaves a table with the wrong shape alone."""
    path = tmp_path / "store.db"
    connect(path).close()
    conn = sqlite3.connect(path)
    add_retired_columns(conn)
    conn.execute("DROP TABLE fit_labels")
    conn.execute("CREATE TABLE fit_labels (fit_id INTEGER NOT NULL, set_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match=r"fit_labels still lacks label"):
        upgrade_store(path)
    # The refusal rolls back, so the store keeps the shape it had and gains no ledger row.
    assert "label" not in [row[1] for row in rows(path, "PRAGMA table_info(fit_labels)")]
    tables = rows(path, "SELECT name FROM sqlite_master WHERE type = 'table'")
    assert ("schema_migrations",) not in tables


def test_a_null_row_stops_the_upgrade_before_the_backup_is_written(tmp_path: Path) -> None:
    """The refusal says to run the upgrade again, and a leftover backup would block that."""
    path = make_v1_store(tmp_path / "store.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error)"
        " VALUES ('nhl', 'https://example.test/a', NULL, 1, NULL)"
    )
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match=r"source_reads holds 1 rows with a NULL fetched_at"):
        upgrade_store(path)
    assert list(tmp_path.glob("*.bak")) == []


def test_the_script_splitter_keeps_each_statement_whole() -> None:
    """executescript commits, so a script runs one statement at a time inside the transaction."""
    assert sql_text.statements("CREATE TABLE a (x TEXT);\nCREATE VIEW b AS SELECT 1;\n") == (
        "CREATE TABLE a (x TEXT);",
        "CREATE VIEW b AS SELECT 1;",
    )
    assert sql_text.statements("\n-- a comment only\n") == ()
    assert sql_text.statements("\n/* a block comment only */\n") == ()
    assert sql_text.statements("CREATE TABLE a (x TEXT);\n/* a note */") == (
        "CREATE TABLE a (x TEXT);",
    )


def test_the_script_splitter_refuses_a_statement_with_no_end() -> None:
    """A missing semicolon would run as a silent no-op, so the split names it."""
    with pytest.raises(StoreError, match="the last statement is incomplete"):
        sql_text.statements("CREATE TABLE a (x TEXT)")


def test_a_stamp_ahead_of_the_shape_runs_every_change_again(tmp_path: Path) -> None:
    """The run 44345179216 state: the stamp says 4 and the tables hold the version 1 shape."""
    path = make_v1_store(tmp_path / "store.db")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 4")
    conn.commit()
    conn.close()
    report = upgrade_store(path)
    assert (report.from_version, report.changed) == (1, True)
    assert report.note is not None
    assert "version 2 needs mp_team_games.shot_attempts_for" in report.note
    assert version_of(path) == SCHEMA_VERSION
    assert "attempts_known" in [row[1] for row in rows(path, "PRAGMA table_info(mp_team_games)")]


def test_a_stamp_ahead_by_one_reruns_only_the_change_it_lost(tmp_path: Path) -> None:
    """A store stamped 5 that lacks the NOT NULL of version 5 earns 4 and reruns the 4->5 step."""
    path = tmp_path / "store.db"
    connect(path).close()
    conn = sqlite3.connect(path)
    add_retired_columns(conn)
    conn.execute("DROP TABLE source_reads")
    conn.execute(
        "CREATE TABLE source_reads (source TEXT, url TEXT, fetched_at TEXT, ok INTEGER, error TEXT)"
    )
    conn.execute(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error)"
        " VALUES ('nhl', 'https://example.test/a', '2026-01-01T00:00:00Z', 1, NULL)"
    )
    conn.commit()
    conn.close()
    report = upgrade_store(path)
    assert (report.from_version, report.changed) == (4, True)
    assert report.note is not None
    assert "version 5 needs source_reads.fetched_at" in report.note
    pragma = "SELECT name, \"notnull\" FROM pragma_table_info('source_reads')"
    assert ("fetched_at", 1) in rows(path, pragma)
    assert rows(path, "SELECT source, url FROM source_reads") == [("nhl", "https://example.test/a")]
    assert version_of(path) == SCHEMA_VERSION


def test_the_reread_flag_makes_moneypuck_stale_even_when_fresh() -> None:
    from typing import cast

    from hockey_model.sources.http import HttpClient
    from hockey_model.store.refresh_context import RefreshRun
    from hockey_model.store.refresh_other import _stale

    run = RefreshRun(
        conn=cast(sqlite3.Connection, None),
        client=cast(HttpClient, None),
        now=datetime(2026, 11, 6, tzinfo=UTC),
        reread_moneypuck=True,
    )
    assert _stale(run, "https://example.test/teams.csv") is True
