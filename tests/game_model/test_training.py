"""Tests for the training rows."""

import math
import sqlite3

import pytest
from seeding import add_game, add_goalie, add_goalie_form, utc

from hockey_model.game_model.elo import EloBook, replay
from hockey_model.game_model.features import FEATURES, GoalieMode
from hockey_model.game_model.training import (
    game_input,
    last_trained_season,
    last_trained_start,
    training_rows,
)
from hockey_model.store.reads import final_games_before


def logit(p: float) -> float:
    return math.log(p / (1 - p))


def seed(store: sqlite3.Connection, last_score: tuple[int, int] = (3, 2)) -> None:
    add_game(store, 1, "2014-11-01T00:00", season=20142015)
    add_game(store, 2, "2015-11-01T00:00", season=20152016, score=(1, 4))
    add_game(store, 3, "2015-11-03T00:00", season=20152016, home="BOS", away="TOR")
    add_game(store, 4, "2016-11-03T00:00", season=20162017, score=last_score)


def test_rows_cover_the_asked_seasons_only(store: sqlite3.Connection) -> None:
    seed(store)
    rows = training_rows(store, 20152016, 20152016, GoalieMode.ACTUAL, names=FEATURES)
    assert [r.game.game_id for r in rows] == [2, 3]


def test_rows_before_the_first_season_still_move_the_elo(store: sqlite3.Connection) -> None:
    seed(store)
    first = training_rows(store, 20152016, 20152016, GoalieMode.ACTUAL, names=FEATURES)[0]
    burn_in = replay(
        g for g in final_games_before(store, utc("2020-01-01T00:00")) if g.game_id == 1
    )
    assert first.features.elo_logit == pytest.approx(logit(burn_in.home_win_chance("TOR", "BOS")))


def test_the_first_row_uses_the_initial_ratings_without_burn_in(store: sqlite3.Connection) -> None:
    add_game(store, 2, "2015-11-01T00:00", season=20152016)
    row = training_rows(store, 20152016, 20152016, GoalieMode.ACTUAL, names=FEATURES)[0]
    assert row.features.elo_logit == pytest.approx(logit(EloBook().home_win_chance("TOR", "BOS")))


def test_the_home_result_is_the_label(store: sqlite3.Connection) -> None:
    seed(store)
    rows = training_rows(store, 20152016, 20152016, GoalieMode.ACTUAL, names=FEATURES)
    assert [r.home_won for r in rows] == [False, True]


def test_a_row_does_not_see_its_own_result(store: sqlite3.Connection) -> None:
    seed(store, last_score=(5, 0))
    first = training_rows(store, 20162017, 20162017, GoalieMode.ACTUAL, names=FEATURES)[0]
    store.execute("UPDATE games SET home_score = 0, away_score = 5 WHERE game_id = 4")
    second = training_rows(store, 20162017, 20162017, GoalieMode.ACTUAL, names=FEATURES)[0]
    assert first.features.elo_logit == second.features.elo_logit
    assert first.home_won != second.home_won


def test_a_later_result_moves_the_next_row(store: sqlite3.Connection) -> None:
    seed(store)
    before = training_rows(store, 20162017, 20162017, GoalieMode.ACTUAL, names=FEATURES)[0]
    store.execute("UPDATE games SET home_score = 0, away_score = 9 WHERE game_id = 2")
    after = training_rows(store, 20162017, 20162017, GoalieMode.ACTUAL, names=FEATURES)[0]
    assert before.features.elo_logit != after.features.elo_logit


def test_game_input_copies_the_stored_game(store: sqlite3.Connection) -> None:
    seed(store)
    row = training_rows(store, 20152016, 20152016, GoalieMode.ACTUAL, names=FEATURES)[0]
    game = game_input(row.game)
    assert (game.game_id, game.home, game.away, game.start) == (2, "TOR", "BOS", row.game.start)


def test_guess_rows_use_the_guessed_goalie_not_the_boxscore(store: sqlite3.Connection) -> None:
    for number in (1, 2, 3):
        add_game(store, number, f"2015-11-0{number}T00:00", season=20152016)
        add_goalie(store, number, "TOR", 11, "C. Ingram")
        add_goalie_form(store, number, 11, 3.0, 0)
    add_goalie_form(store, 3, 12, 0.0, 3)
    add_game(store, 4, "2015-11-04T00:00", season=20152016)
    add_goalie(store, 4, "TOR", 12, "J. Woll")
    # The away side has a neutral form in every game, so the differential is the home goalie.
    for number in (1, 2, 3, 4):
        add_goalie(store, number, "BOS", 21, "B. Away")
        add_goalie_form(store, number, 21, 0.0, 0)
    guess = training_rows(store, 20152016, 20152016, GoalieMode.GUESS, names=FEATURES)[-1]
    actual = training_rows(store, 20152016, 20152016, GoalieMode.ACTUAL, names=FEATURES)[-1]
    assert guess.game.game_id == actual.game.game_id == 4
    assert guess.features.values["goalie_gsax"] == pytest.approx(3.0)
    assert actual.features.values["goalie_gsax"] == pytest.approx(-3.0)
    assert all(m.kind.value != "late_run" for m in guess.features.missing)


def test_the_last_trained_season_is_the_newest_final_season(store: sqlite3.Connection) -> None:
    seed(store)
    assert last_trained_season(store) == 20162017


def test_a_store_whose_only_season_is_the_first_fit_season_trains(
    store: sqlite3.Connection,
) -> None:
    """FIT_FIRST itself is trainable, so the floor comparison must not drop it."""
    add_game(store, 1, "2015-11-01T00:00", season=20152016)
    assert last_trained_season(store) == 20152016


def test_a_scheduled_game_does_not_set_the_last_trained_season(store: sqlite3.Connection) -> None:
    seed(store)
    add_game(store, 5, "2017-12-01T00:00", season=20172018, state="FUT")
    assert last_trained_season(store) == 20162017


def test_an_empty_store_has_no_last_trained_season(store: sqlite3.Connection) -> None:
    assert last_trained_season(store) is None


def test_a_store_before_the_first_fit_season_has_no_last_trained_season(
    store: sqlite3.Connection,
) -> None:
    add_game(store, 1, "2014-11-01T00:00", season=20142015)
    assert last_trained_season(store) is None


def test_the_last_trained_start_is_the_newest_final_start_of_the_seasons(
    store: sqlite3.Connection,
) -> None:
    seed(store)
    assert last_trained_start(store, 20162017) == utc("2016-11-03T00:00")


def test_the_last_trained_start_keeps_to_the_asked_seasons(store: sqlite3.Connection) -> None:
    seed(store)
    assert last_trained_start(store, 20152016) == utc("2015-11-03T00:00")


def test_a_scheduled_game_does_not_set_the_last_trained_start(store: sqlite3.Connection) -> None:
    seed(store)
    add_game(store, 5, "2017-12-01T00:00", season=20172018, state="FUT")
    assert last_trained_start(store, 20172018) == utc("2016-11-03T00:00")


def test_an_empty_store_has_no_last_trained_start(store: sqlite3.Connection) -> None:
    assert last_trained_start(store, 20152016) is None
