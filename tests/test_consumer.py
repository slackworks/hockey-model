"""Tests for the read-only consumer API (#80)."""

import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from seeding import FETCHED, add_game, add_goalie, add_injury, add_team_form, utc

import hockey_model
from hockey_model.consumer import open_store
from hockey_model.consumer_errors import (
    StoreMissingError,
    StoreVersionError,
    UnknownFitError,
    UnknownGameError,
)
from hockey_model.consumer_types import FULL_GAME_WINNER, RESTATEMENT, GameInfo
from hockey_model.game import TrainingInfo, TrainingMembership
from hockey_model.game_model.logistic import ELO_ONLY, LogisticFit
from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl import GameRow
from hockey_model.store.db import SCHEMA_VERSION, connect
from hockey_model.store.fits import FitRecord, FitRef, save_fit
from hockey_model.store.nhl_writes import write_games
from hockey_model.store.source_reads import record_read
from hockey_model.types import HockeyModelError

DAY = date(2026, 1, 20)
NOW = utc("2026-01-20T12:00")
T0 = utc("2026-01-01T00:00")
# A read time after the seed's, so a later write becomes the newest read of its game.
LATER = FETCHED + timedelta(days=1)


def _research_fit() -> LogisticFit:
    return LogisticFit(("xg_form",), 0.1, 0.9, {"xg_form": 0.02})


def _write(path: Path, fill: Callable[[sqlite3.Connection], object]) -> None:
    """Add rows to the store at ``path`` and commit them, as a producer does."""
    conn = connect(path)
    try:
        fill(conn)
        conn.commit()
    finally:
        conn.close()


def _seed(path: Path) -> None:
    """Write a store with one final game, a fit labelled ``live``, and one future game."""

    def fill(conn: sqlite3.Connection) -> None:
        add_game(conn, 1, "2026-01-10T00:00", "TOR", "BOS", score=(4, 1))
        add_goalie(conn, 1, "TOR", 8478402, "Joseph Woll")
        add_team_form(conn, 1, "TOR", 3.5, 2.0)
        add_team_form(conn, 1, "BOS", 2.0, 3.0)
        save_fit(conn, FitRecord(ELO_ONLY, 20252026, T0, label="live"))
        add_game(conn, 2, "2026-01-20T23:00", "TOR", "BOS", state="FUT")

    _write(path, fill)


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    path = tmp_path / "store.db"
    _seed(path)
    return path


def _file_key(path: Path) -> tuple[int, int]:
    found = path.stat()
    return found.st_mtime_ns, found.st_size


class _ReadOnlyView:
    """A connection wrapper that fails the test when the consumer path runs a write.

    It records the real connection, so a test can try a write on the consumer's own handle.
    """

    opened: list[sqlite3.Connection] = []

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        _ReadOnlyView.opened.append(conn)

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        head = sql.lstrip().split(maxsplit=1)[0].upper()
        if head not in {"SELECT", "PRAGMA", "WITH"}:
            raise AssertionError(f"the consumer path ran a write: {sql}")
        return self._conn.execute(sql, *args)

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _no_fetch(self: HttpClient, source: str, url: str) -> bytes:
    raise AssertionError(f"the consumer path fetched {url}")


def test_reading_and_predicting_writes_nothing_and_fetches_nothing(
    store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _file_key(store_path)
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _ReadOnlyView(real_connect(*a, **k)))
    monkeypatch.setattr(HttpClient, "get", _no_fetch)
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        assert consumer.game(1).home == "TOR"
        assert [info.game_id for info in consumer.games_on_day(DAY)] == [2]
        assert consumer.skater_games(1) == ()
        assert consumer.goalie_games(1)[0].nhl_id == 8478402
        assert consumer.team_xg_games("TOR", before=NOW, count=1)[0].xg_for == 3.5
        assert consumer.goalie_xg_games(8478402, before=NOW, count=1) == ()
        assert consumer.injuries(as_of=NOW) == ()
        assert consumer.starter(2, "TOR", as_of=NOW).team == "TOR"
        assert consumer.predict(2, as_of=NOW).outcome == FULL_GAME_WINNER
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            _ReadOnlyView.opened[-1].execute("INSERT INTO games (game_id) VALUES (99)")
    assert _file_key(store_path) == before


def test_a_missing_path_raises_and_creates_no_file(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"
    with pytest.raises(StoreMissingError, match="no store file there.*hockey-model refresh"):
        open_store(path, fit=FitRef(label="live"))
    assert not path.exists()


def test_a_prediction_names_the_training_membership(store_path: Path) -> None:
    """The seed fit trained through 2025-26, and both seeded games are in that season."""
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        result = consumer.predict(2, as_of=NOW)
    assert result.training == TrainingInfo(1, 20252026, TrainingMembership.IN_SAMPLE)


def test_a_labelled_fit_wins_over_a_newer_unlabelled_fit(store_path: Path) -> None:
    research: list[int] = []
    _write(
        store_path,
        lambda conn: research.append(
            save_fit(conn, FitRecord(_research_fit(), 20252026, T0 + timedelta(days=1)))
        ),
    )
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        assert consumer.fit.labels == ("live",)
        assert consumer.predict(2, as_of=NOW).fit.features == ()
    with open_store(store_path, fit=FitRef(fit_id=research[0])) as consumer:
        assert consumer.fit.features == ("xg_form",)


def _write_while_open(conn: sqlite3.Connection) -> None:
    """Store a newer read of game 1, a new game 3, and a newer fit that carries the label."""
    add_game(conn, 1, "2026-01-10T00:00", "TOR", "BOS", score=(5, 1), fetched_at=LATER)
    add_game(conn, 3, "2026-01-21T00:00", "EDM", "CGY", state="FUT")
    save_fit(conn, FitRecord(_research_fit(), 20252026, T0 + timedelta(days=2), label="live"))


def test_a_new_write_is_invisible_until_the_snapshot_refreshes(store_path: Path) -> None:
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        pinned = consumer.fit
        _write(store_path, _write_while_open)
        assert consumer.game(1).home_score == 4
        with pytest.raises(UnknownGameError, match="game 3"):
            consumer.game(3)
        consumer.refresh_snapshot()
        assert consumer.game(1).home_score == 5
        assert consumer.game(3).game_id == 3
        assert consumer.fit == pinned


def test_a_postponed_game_keeps_its_own_state(store_path: Path) -> None:
    _write(
        store_path,
        lambda conn: add_game(conn, 4, "2026-01-20T23:00", "TOR", "BOS", state="PPD"),
    )
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        info = consumer.game(4)
    assert (info.state, info.final, info.last_period) == ("PPD", False, None)


def _reschedule(conn: sqlite3.Connection) -> None:
    add_game(conn, 5, "2026-01-20T23:00", "TOR", "BOS", state="FUT")
    add_game(conn, 5, "2026-02-03T23:00", "TOR", "BOS", state="FUT", fetched_at=LATER)


def test_a_rescheduled_game_reads_its_newest_read(store_path: Path) -> None:
    _write(store_path, _reschedule)
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        assert consumer.game(5).start == utc("2026-02-03T23:00")


def _shootout(conn: sqlite3.Connection) -> None:
    row = GameRow(6, 20252026, 2, utc("2026-01-20T23:00"), "TOR", "BOS", "FINAL", 2, 1, "SO")
    write_games(conn, [row], utc("2026-01-21T03:00"))


def test_a_shootout_final_keeps_its_last_period(store_path: Path) -> None:
    _write(store_path, _shootout)
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        info = consumer.game(6)
    assert (info.state, info.final, info.last_period) == ("FINAL", True, "SO")


def test_a_prediction_after_the_start_is_marked_late(store_path: Path) -> None:
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        early = consumer.predict(2, as_of=NOW)
        late = consumer.predict(2, as_of=utc("2026-01-21T00:00"))
    assert early.made_after_start is False
    assert late.made_after_start is True
    assert late.reconstruction == RESTATEMENT
    assert late.fit == early.fit
    assert late.store.schema_version == SCHEMA_VERSION


def test_home_and_away_stay_in_the_stored_order(store_path: Path) -> None:
    _write(
        store_path,
        lambda conn: add_game(conn, 7, "2026-01-20T23:00", "BOS", "TOR", state="FUT"),
    )
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        straight = consumer.game(2)
        reversed_game = consumer.game(7)
        result = consumer.predict(7, as_of=NOW)
    assert (straight.home, straight.away) == ("TOR", "BOS")
    assert (reversed_game.home, reversed_game.away) == ("BOS", "TOR")
    assert (result.game.home, result.game.away) == ("BOS", "TOR")


def test_a_wrong_schema_version_raises(store_path: Path) -> None:
    conn = sqlite3.connect(store_path)
    conn.execute("UPDATE schema_version SET version = 2")
    conn.commit()
    conn.close()
    with pytest.raises(StoreVersionError, match="schema version 2 is not version 3.*upgrade"):
        open_store(store_path, fit=FitRef(label="live"))


def test_a_missing_view_raises(store_path: Path) -> None:
    conn = sqlite3.connect(store_path)
    conn.execute("DROP VIEW latest_mp_goalie_games")
    conn.commit()
    conn.close()
    with pytest.raises(StoreVersionError, match="latest_mp_goalie_games.*upgrade"):
        open_store(store_path, fit=FitRef(label="live"))


def test_a_file_that_is_not_a_store_raises(tmp_path: Path) -> None:
    path = tmp_path / "notes.db"
    path.write_bytes(b"not a database")
    with pytest.raises(StoreVersionError, match="not a SQLite store.*hockey-model store"):
        open_store(path, fit=FitRef(label="live"))


@pytest.mark.skipif(os.name != "posix", reason="a directory permission bit is a POSIX rule")
def test_a_wal_store_in_a_read_only_directory_names_the_directory() -> None:
    # SQLite cannot create the -wal and -shm files beside a WAL store in a read-only
    # directory, so the open fails with SQLITE_READONLY_DIRECTORY. The file itself is fine,
    # and the fix names the directory, not the file.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "store.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(StoreVersionError, match="directory write permission"):
                open_store(path, fit=FitRef(label="live"))
        finally:
            os.chmod(folder, stat.S_IRWXU)


def test_an_unknown_game_raises(store_path: Path) -> None:
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        with pytest.raises(UnknownGameError, match="game 99.*hockey-model refresh"):
            consumer.game(99)


def test_an_unknown_fit_raises(store_path: Path) -> None:
    with pytest.raises(UnknownFitError, match="fit 99"):
        open_store(store_path, fit=FitRef(fit_id=99))


def test_a_naive_time_is_refused(store_path: Path) -> None:
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        with pytest.raises(HockeyModelError, match="as_of"):
            consumer.predict(2, as_of=datetime(2026, 1, 20, 12, 0))


def _take_an_extra_slate(conn: sqlite3.Connection) -> None:
    # Game 2 of the seed is one game of the slate, so 15 rows here make a slate of 16.
    for game_id in range(100, 115):
        add_game(conn, game_id, "2026-01-20T23:00", "TOR", "BOS", state="FUT")


def test_a_full_slate_of_sixteen_games_is_read_in_one_snapshot(store_path: Path) -> None:
    _write(store_path, _take_an_extra_slate)
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        slate = consumer.games_on_day(DAY)
        results = [consumer.predict(info.game_id, as_of=NOW) for info in slate]
    assert [info.game_id for info in slate] == [2, *range(100, 115)]
    assert len(results) == 16


def _injury_list(conn: sqlite3.Connection) -> None:
    add_injury(conn, "TOR", "Auston Matthews", "OUT", "2026-01-20T09:00")


def test_the_newest_injury_list_before_the_cutoff_is_read(store_path: Path) -> None:
    _write(store_path, _injury_list)
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        early = consumer.injuries(as_of=utc("2026-01-20T08:00"))
        late = consumer.injuries(as_of=NOW)
        assert consumer.store.path == str(store_path)
    assert early == ()
    (line,) = late
    assert (line.team, line.player_name, line.status) == ("TOR", "Auston Matthews", "OUT")
    assert line.return_date is None
    assert line.read_at == utc("2026-01-20T09:00")


def test_an_injury_list_older_than_the_live_feed_limit_is_not_used(store_path: Path) -> None:
    # The store read the list three days before the cutoff. It can still name a healed player,
    # so the read returns no line. StoreInfo names the read, so a caller sees why.
    _write(
        store_path,
        lambda conn: add_injury(conn, "TOR", "Auston Matthews", "OUT", "2026-01-17T12:00"),
    )
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        assert consumer.injuries(as_of=NOW) == ()
        reads = dict(consumer.store.newest_reads)
        assert reads["espn_injuries"] == utc("2026-01-17T12:00")


def test_store_info_lists_a_source_that_every_read_of_failed(store_path: Path) -> None:
    # `moneypuck` has one failed read and no good one. The source is named, with no time, so a
    # caller can tell it apart from a source that the store never read.
    _write(
        store_path,
        lambda conn: record_read(conn, "moneypuck", "u", NOW, error="HTTP 503"),
    )
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        reads = dict(consumer.store.newest_reads)
    assert reads["moneypuck"] is None
    assert "nhl_scores" not in reads


@pytest.mark.parametrize("count", [-1])
def test_a_negative_count_is_refused(store_path: Path, count: int) -> None:
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        with pytest.raises(ValueError, match="count must be 0 or more"):
            consumer.team_xg_games("TOR", before=NOW, count=count)
        with pytest.raises(ValueError, match="count must be 0 or more"):
            consumer.goalie_xg_games(8478402, before=NOW, count=count)


def test_a_line_read_of_an_unknown_game_raises(store_path: Path) -> None:
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        with pytest.raises(UnknownGameError, match="game 99"):
            consumer.skater_games(99)
        with pytest.raises(UnknownGameError, match="game 99"):
            consumer.goalie_games(99)


def test_a_starter_of_a_team_outside_the_game_raises(store_path: Path) -> None:
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        with pytest.raises(ValueError, match="team 'EDM' is not in the game.*'TOR' or 'BOS'"):
            consumer.starter(2, "EDM", as_of=NOW)


def test_a_goalie_xg_row_with_a_missing_value_is_skipped(store_path: Path) -> None:
    # A MoneyPuck row can carry no expected goals for one goalie. The result type needs a
    # number, so the row is skipped, as the team reader skips it.
    def fill(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO mp_goalie_games (game_id, nhl_id, team, xg_against, goals_against,"
            " fetched_at) VALUES (1, 8478402, 'TOR', NULL, 3, ?)",
            ("2026-01-11T00:00:00.000000+00:00",),
        )

    _write(store_path, fill)
    with open_store(store_path, fit=FitRef(label="live")) as consumer:
        assert consumer.goalie_xg_games(8478402, before=NOW, count=1) == ()


@given(
    st.text(
        alphabet=st.characters(
            whitelist_categories=None, min_codepoint=0x20, max_codepoint=0x7E
        ).filter(lambda char: char not in "/\\"),
        min_size=1,
        max_size=24,
    )
)
def test_a_store_path_with_any_ascii_name_opens(name: str) -> None:
    """The connection URI quotes the path, so a name with `?`, `#`, or `%` is a plain name."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"{name}.db"
        _seed(path)
        with open_store(path, fit=FitRef(label="live")) as consumer:
            assert consumer.game(1).game_id == 1
            assert consumer.store.path == str(path)


def test_close_ends_the_transaction_without_a_context_manager(store_path: Path) -> None:
    consumer = open_store(store_path, fit=FitRef(label="live"))
    assert consumer.game(1).game_id == 1
    consumer.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        consumer.game(1)


def test_a_game_info_needs_final_iff_its_state_is_final() -> None:
    stored = GameInfo(1, 20252026, 2, NOW, "TOR", "BOS", "FUT", False, None, None, None, T0)
    assert stored.final is False
    with pytest.raises(ValueError, match="final is True but state is 'FUT'"):
        replace(stored, final=True)


def test_an_empty_schema_version_raises(store_path: Path) -> None:
    conn = sqlite3.connect(store_path)
    conn.execute("DELETE FROM schema_version")
    conn.commit()
    conn.close()
    with pytest.raises(StoreVersionError, match="schema_version is empty"):
        open_store(store_path, fit=FitRef(label="live"))


@pytest.mark.parametrize(
    "name",
    [
        "FULL_GAME_WINNER",
        "LIVE_LABEL",
        "RESTATEMENT",
        "ConsumerPrediction",
        "ConsumerStore",
        "FitInfo",
        "FitRef",
        "GameInfo",
        "GoalieLine",
        "GoalieXgLine",
        "InjuryLine",
        "SkaterLine",
        "StarterInfo",
        "StoreInfo",
        "StoreMissingError",
        "StoreVersionError",
        "TeamXgLine",
        "UnknownFitError",
        "UnknownGameError",
        "UnsupportedCapabilityError",
        "open_store",
    ],
)
def test_every_consumer_name_is_exported(name: str) -> None:
    assert name in hockey_model.__all__
    assert getattr(hockey_model, name) is not None
