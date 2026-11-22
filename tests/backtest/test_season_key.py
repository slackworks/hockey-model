"""The key of a cached season: it changes when any input of the season's rows changes."""

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from seeding import FETCHED, add_goalie
from test_runner_store import PLAN, seed

from hockey_model.backtest.season_key import COVERED_TABLES, season_keys, source_digest
from hockey_model.game_model.features import FEATURES, GoalieMode
from hockey_model.game_model.training import training_rows
from hockey_model.store.timestamps import iso_utc, parse_iso_utc

SEASONS = range(1, 8)


@pytest.fixture
def seeded(store: sqlite3.Connection) -> sqlite3.Connection:
    seed(store, SEASONS)
    store.commit()
    return store


def _keys(conn: sqlite3.Connection, names: tuple[str, ...] = FEATURES) -> dict[int, str]:
    return season_keys(conn, PLAN.burn_in, names)


def _changed(before: dict[int, str], after: dict[int, str]) -> list[int]:
    assert before.keys() == after.keys()
    return [season for season in before if before[season] != after[season]]


def test_a_key_is_stable_and_there_is_one_for_each_season(seeded: sqlite3.Connection) -> None:
    keys = _keys(seeded)
    assert sorted(keys) == list(SEASONS)
    assert len(set(keys.values())) == len(SEASONS)
    assert keys == _keys(seeded)


def _first_game_of(conn: sqlite3.Connection, season: int) -> int:
    (game_id,) = conn.execute(
        "SELECT MIN(game_id) FROM games WHERE season = ?", (season,)
    ).fetchone()
    return int(game_id)


# Each statement changes one stored value that the rows of the season can read. ``{g}`` is the
# first game of season 3.
_CHANGES = {
    "latest_final_games": "UPDATE games SET home_score = home_score + 1 WHERE game_id = {g}",
    "latest_mp_team_games": "UPDATE mp_team_games SET xg_for = xg_for + 1 WHERE game_id = {g}",
    "latest_mp_goalie_games": (
        "UPDATE mp_goalie_games SET xg_against = xg_against + 1 WHERE game_id = {g}"
    ),
    "goalie_games": "UPDATE goalie_games SET started = 0 WHERE game_id = {g}",
}


def test_every_covered_table_has_a_change_in_this_test() -> None:
    # A new table in the key needs a case here, so a table cannot sit in the key unchecked.
    assert set(_CHANGES) | {"latest_players", "source_reads", "injury_reads"} == set(COVERED_TABLES)


@pytest.mark.parametrize("table", sorted(_CHANGES))
def test_a_changed_value_changes_its_season_and_every_later_season(
    seeded: sqlite3.Connection, table: str
) -> None:
    before = _keys(seeded)
    seeded.execute(_CHANGES[table].format(g=_first_game_of(seeded, 3)))
    seeded.commit()
    assert _changed(before, _keys(seeded)) == [3, 4, 5, 6, 7]


def test_a_newer_read_of_a_game_changes_the_key(seeded: sqlite3.Connection) -> None:
    before = _keys(seeded)
    game_id = _first_game_of(seeded, 5)
    seeded.execute(
        "INSERT INTO games SELECT game_id, season, game_type, start_utc, home, away, state,"
        " home_score + 2, away_score, last_period, ? FROM games WHERE game_id = ?",
        (iso_utc(FETCHED.replace(year=FETCHED.year + 1)), game_id),
    )
    seeded.commit()
    assert _changed(before, _keys(seeded)) == [5, 6, 7]


def test_a_later_live_read_of_a_final_game_changes_no_key(seeded: sqlite3.Connection) -> None:
    before = _keys(seeded)
    seeded.execute(
        "INSERT INTO games SELECT game_id, season, game_type, start_utc, home, away, 'LIVE',"
        " NULL, NULL, NULL, ? FROM games WHERE game_id = ?",
        (iso_utc(FETCHED.replace(year=FETCHED.year + 1)), _first_game_of(seeded, 7)),
    )
    seeded.commit()
    # The view of final games keeps the newest final read, and no row reads the live one.
    assert _changed(before, _keys(seeded)) == []


def test_a_goalie_of_the_catalog_changes_every_key(seeded: sqlite3.Connection) -> None:
    before = _keys(seeded)
    seeded.execute("UPDATE players SET team = 'MTL' WHERE nhl_id = 11")
    seeded.commit()
    assert _changed(before, _keys(seeded)) == list(SEASONS)


def test_a_new_goalie_changes_every_key(seeded: sqlite3.Connection) -> None:
    before = _keys(seeded)
    add_goalie(seeded, _first_game_of(seeded, 7), "TOR", 99, "New Goalie", started=False)
    seeded.commit()
    assert _changed(before, _keys(seeded)) == list(SEASONS)


def test_a_skater_who_never_played_goal_changes_no_key(seeded: sqlite3.Connection) -> None:
    before = _keys(seeded)
    seeded.execute(
        "INSERT INTO players VALUES (777, 'Some Skater', 'C', 'TOR', ?)", (iso_utc(FETCHED),)
    )
    seeded.commit()
    assert _changed(before, _keys(seeded)) == []


_AFTER_ALL = "2030-01-01T00:00:00.000000+00:00"


def _read(
    conn: sqlite3.Connection, stamp: str, *, source: str = "espn_injuries", ok: int = 1
) -> None:
    conn.execute("INSERT INTO source_reads VALUES (?, 'u', ?, ?, NULL, 0)", (source, stamp, ok))
    conn.commit()


def _injury_row(conn: sqlite3.Connection, stamp: str) -> None:
    conn.execute("INSERT INTO injury_reads VALUES ('TOR', 'Goalie TOR', 'out', NULL, ?)", (stamp,))
    conn.commit()


def _season_end(conn: sqlite3.Connection, season: int) -> datetime:
    """Return the start of the last final game of ``season``."""
    (text,) = conn.execute(
        "SELECT MAX(start_utc) FROM latest_final_games WHERE season = ?", (season,)
    ).fetchone()
    return parse_iso_utc(text)


_WRITERS = {"source_reads": _read, "injury_reads": _injury_row}
_MICROSECOND = timedelta(microseconds=1)


@pytest.mark.parametrize("table", sorted(_WRITERS))
@pytest.mark.parametrize(
    ("offset", "changed"),
    [
        (-_MICROSECOND, [3, 4, 5, 6, 7]),
        (timedelta(0), [3, 4, 5, 6, 7]),
        (_MICROSECOND, [4, 5, 6, 7]),
    ],
    ids=["a microsecond before the last start", "at the last start", "a microsecond after"],
)
def test_an_injury_read_belongs_to_the_first_season_that_ends_at_or_after_it(
    seeded: sqlite3.Connection, table: str, offset: timedelta, changed: list[int]
) -> None:
    before = _keys(seeded)
    _WRITERS[table](seeded, iso_utc(_season_end(seeded, 3) + offset))
    assert _changed(before, _keys(seeded)) == changed


@pytest.mark.parametrize("table", sorted(_WRITERS))
def test_an_injury_read_after_the_last_game_changes_no_key(
    seeded: sqlite3.Connection, table: str
) -> None:
    before = _keys(seeded)
    _WRITERS[table](seeded, iso_utc(_season_end(seeded, 7) + _MICROSECOND))
    _WRITERS[table](seeded, _AFTER_ALL)
    assert _changed(before, _keys(seeded)) == []


def test_a_failed_or_other_read_changes_no_key(seeded: sqlite3.Connection) -> None:
    before = _keys(seeded)
    stamp = iso_utc(_season_end(seeded, 4))
    _read(seeded, stamp, ok=0)
    _read(seeded, stamp, source="nhl_score")
    assert _changed(before, _keys(seeded)) == []


def test_the_status_and_return_date_of_an_injury_are_part_of_the_key(
    seeded: sqlite3.Connection,
) -> None:
    stamp = iso_utc(_season_end(seeded, 4))
    _injury_row(seeded, stamp)
    before = _keys(seeded)
    seeded.execute("UPDATE injury_reads SET status = 'day-to-day'")
    seeded.commit()
    assert _changed(before, _keys(seeded)) == [4, 5, 6, 7]
    after_status = _keys(seeded)
    seeded.execute("UPDATE injury_reads SET return_date = '2030-01-01'")
    seeded.commit()
    assert _changed(after_status, _keys(seeded)) == [4, 5, 6, 7]


def test_the_features_and_the_burn_in_are_part_of_the_key(seeded: sqlite3.Connection) -> None:
    base = _keys(seeded)
    assert _changed(base, _keys(seeded, ("xg_form", "rest"))) == list(SEASONS)
    assert _changed(base, season_keys(seeded, PLAN.burn_in + 1, FEATURES)) == list(SEASONS)


def test_the_order_of_the_features_is_part_of_the_key(seeded: sqlite3.Connection) -> None:
    assert _changed(_keys(seeded), _keys(seeded, tuple(reversed(FEATURES)))) == list(SEASONS)


def test_an_empty_store_has_no_key(store: sqlite3.Connection) -> None:
    assert _keys(store) == {}


_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_]+)", re.IGNORECASE)


@pytest.mark.parametrize("mode", [GoalieMode.ACTUAL, GoalieMode.GUESS])
def test_a_build_reads_only_tables_that_the_key_covers(
    seeded: sqlite3.Connection, mode: GoalieMode
) -> None:
    """A table that the build starts to read, and the key leaves out, fails this test."""
    statements: list[str] = []
    seeded.set_trace_callback(statements.append)
    try:
        training_rows(seeded, 2, 7, mode, PLAN.burn_in, names=FEATURES)
    finally:
        seeded.set_trace_callback(None)
    read = {name.lower() for text in statements for name in _TABLE.findall(text)}
    assert read
    assert read <= set(COVERED_TABLES), sorted(read - set(COVERED_TABLES))


def test_the_source_digest_follows_the_code_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "views.sql").write_text("SELECT 1;\n")
    (tmp_path / "notes.txt").write_text("one\n")
    base = source_digest(tmp_path)
    assert source_digest(tmp_path) == base
    (tmp_path / "notes.txt").write_text("two\n")
    assert source_digest(tmp_path) == base
    (tmp_path / "a.py").write_text("x = 2\n")
    changed = source_digest(tmp_path)
    assert changed != base
    (tmp_path / "views.sql").write_text("SELECT 2;\n")
    assert source_digest(tmp_path) not in {base, changed}


def test_the_source_digest_follows_a_new_and_a_renamed_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    base = source_digest(tmp_path)
    (tmp_path / "a.py").rename(tmp_path / "b.py")
    renamed = source_digest(tmp_path)
    assert renamed != base
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "c.py").write_text("")
    assert source_digest(tmp_path) != renamed


def test_the_source_digest_of_the_package_is_in_the_key(
    seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _keys(seeded)
    monkeypatch.setattr("hockey_model.backtest.season_key.source_fingerprint", lambda: "other")
    assert _changed(before, _keys(seeded)) == list(SEASONS)
