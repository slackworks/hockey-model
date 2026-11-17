"""Tests for the MoneyPuck coverage checks: unmatched rows and form lag."""

import sqlite3
from datetime import UTC, datetime, timedelta

from seeding import FETCHED, add_game, add_team_form, utc

from hockey_model.sources.moneypuck import GoalieGameRow, TeamGameRow
from hockey_model.store.moneypuck_gaps import mp_form_lag, unmatched_moneypuck
from hockey_model.store.moneypuck_writes import write_goalie_games, write_team_games

NOW = utc("2026-01-20T12:00")


def _team_row(game_id: int, team: str = "TOR") -> TeamGameRow:
    return TeamGameRow(game_id, team, "2026-01-10", 2.5, 1.5, 50, 40, 30, 20)


def _goalie_row(game_id: int) -> GoalieGameRow:
    return GoalieGameRow(game_id, 8478402, "TOR", 2.1, 3)


def _stored_game(
    conn: sqlite3.Connection,
    game_id: int,
    stamp: str,
    home: str = "TOR",
    away: str = "BOS",
    state: str = "OFF",
) -> None:
    add_game(conn, game_id, stamp, home, away, state=state, score=(4, 1))


def test_a_final_game_leaves_no_row_unmatched(store: sqlite3.Connection) -> None:
    _stored_game(store, 1, "2026-01-10T00:00")
    write_team_games(store, [_team_row(1)], FETCHED)
    write_goalie_games(store, [_goalie_row(1)], FETCHED)
    assert unmatched_moneypuck(store).is_empty()


def test_a_row_of_an_unknown_game_is_counted(store: sqlite3.Connection) -> None:
    write_team_games(store, [_team_row(9)], FETCHED)
    write_goalie_games(store, [_goalie_row(9)], FETCHED)
    counts = unmatched_moneypuck(store)
    assert (counts.team_unknown, counts.goalie_unknown) == (1, 1)
    assert "1 team-game rows and 1 goalie-game rows match no stored game" in counts.unknown_note()


def test_a_row_of_a_game_that_is_not_final_is_counted_apart(store: sqlite3.Connection) -> None:
    _stored_game(store, 2, "2026-01-10T00:00", state="LIVE")
    write_team_games(store, [_team_row(2)], FETCHED)
    counts = unmatched_moneypuck(store)
    assert (counts.team_unknown, counts.team_not_final) == (0, 1)
    assert "1 team-game rows and 0 goalie-game rows are for a game that is not final" in (
        counts.pending_note()
    )


def test_a_repeated_read_does_not_count_a_row_twice(store: sqlite3.Connection) -> None:
    write_team_games(store, [_team_row(9)], FETCHED)
    write_team_games(store, [_team_row(9)], FETCHED + timedelta(days=7))
    assert unmatched_moneypuck(store).team_unknown == 1


def test_a_later_game_read_resolves_the_row(store: sqlite3.Connection) -> None:
    write_team_games(store, [_team_row(3)], FETCHED)
    assert unmatched_moneypuck(store).team_unknown == 1
    _stored_game(store, 3, "2026-01-10T00:00")
    assert unmatched_moneypuck(store).is_empty()


def test_a_team_with_current_form_has_no_lag(store: sqlite3.Connection) -> None:
    _stored_game(store, 1, "2026-01-10T00:00")
    add_team_form(store, 1, "TOR", 2.5, 1.5)
    assert mp_form_lag(store, "TOR", NOW) is None


def test_a_team_with_stale_form_names_its_lag(store: sqlite3.Connection) -> None:
    _stored_game(store, 1, "2026-01-06T00:00")
    add_team_form(store, 1, "TOR", 2.5, 1.5)
    _stored_game(store, 2, "2026-01-08T00:00")
    _stored_game(store, 3, "2026-01-10T00:00")
    lag = mp_form_lag(store, "TOR", NOW)
    assert lag is not None
    assert (lag.games_behind, lag.newest_moneypuck, lag.newest_final) == (2, 1, 3)
    assert "2 stored games behind" in lag.note()


def test_a_team_with_no_moneypuck_game_has_no_lag(store: sqlite3.Connection) -> None:
    _stored_game(store, 1, "2026-01-10T00:00")
    assert mp_form_lag(store, "TOR", NOW) is None


def test_the_lag_ignores_games_after_the_cutoff(store: sqlite3.Connection) -> None:
    _stored_game(store, 1, "2026-01-10T00:00")
    add_team_form(store, 1, "TOR", 2.5, 1.5)
    _stored_game(store, 2, "2026-01-19T00:00")
    early = utc("2026-01-11T12:00")
    assert mp_form_lag(store, "TOR", early) is None


def test_the_lag_compares_within_the_franchise(store: sqlite3.Connection) -> None:
    # ARI moved to UTA. A MoneyPuck row under either code keeps the franchise covered.
    _stored_game(store, 1, "2026-01-10T00:00", home="UTA")
    add_team_form(store, 1, "ARI", 2.5, 1.5)
    assert mp_form_lag(store, "UTA", NOW) is None


def test_one_team_lag_does_not_hide_the_other(store: sqlite3.Connection) -> None:
    _stored_game(store, 1, "2026-01-05T00:00")
    add_team_form(store, 1, "TOR", 2.5, 1.5)
    _stored_game(store, 2, "2026-01-08T00:00")
    add_team_form(store, 2, "BOS", 1.5, 2.5)
    assert mp_form_lag(store, "BOS", NOW) is None
    assert mp_form_lag(store, "TOR", NOW) is not None


def test_the_row_of_the_other_team_does_not_hide_the_lag(store: sqlite3.Connection) -> None:
    # The newest game is played by both teams, but MoneyPuck read only the home side of it.
    # The form reader takes the row by the code in the row, so TOR is still two games back.
    _stored_game(store, 1, "2026-01-06T00:00")
    add_team_form(store, 1, "TOR", 2.5, 1.5)
    _stored_game(store, 2, "2026-01-10T00:00")
    add_team_form(store, 2, "BOS", 1.5, 2.5)
    assert mp_form_lag(store, "BOS", NOW) is None
    lag = mp_form_lag(store, "TOR", NOW)
    assert lag is not None
    assert (lag.games_behind, lag.newest_moneypuck) == (1, 1)


def test_a_cutoff_before_any_stored_game_has_no_lag(store: sqlite3.Connection) -> None:
    _stored_game(store, 1, "2026-01-10T00:00")
    add_team_form(store, 1, "TOR", 2.5, 1.5)
    before = datetime(2025, 12, 1, tzinfo=UTC)
    assert mp_form_lag(store, "TOR", before) is None
