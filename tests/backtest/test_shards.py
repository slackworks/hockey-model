"""Season shards: the Elo from a season boundary and the sharded build equal the full replay."""

import sqlite3
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st
from test_runner_store import PLAN, seed

from hockey_model.backtest.shards import build_sharded
from hockey_model.game_model.elo import EloBook, replay, season_boundaries
from hockey_model.game_model.features import FEATURES, GoalieMode
from hockey_model.game_model.training import training_rows
from hockey_model.sources.nhl import GameRow

TEAMS = ("TOR", "BOS", "MTL", "EDM")


def _game(game_id: int, season: int, home: str, away: str, margin: int) -> GameRow:
    start = datetime(2015, 10, 1, tzinfo=UTC) + timedelta(hours=game_id)
    home_score, away_score = (3 + margin, 3) if margin > 0 else (3, 3 - margin)
    return GameRow(game_id, season, 2, start, home, away, "OFF", home_score, away_score, "REG")


@st.composite
def _histories(draw: st.DrawFn) -> list[GameRow]:
    """Games in start order over 2 to 5 seasons, with decisive results."""
    seasons = draw(st.integers(2, 5))
    games: list[GameRow] = []
    for season in range(1, seasons + 1):
        for _ in range(draw(st.integers(1, 12))):
            home, away = draw(st.permutations(TEAMS))[:2]
            margin = draw(st.sampled_from([-3, -2, -1, 1, 2, 3]))
            games.append(_game(len(games) + 1, season, home, away, margin))
    return games


@given(_histories())
def test_a_replay_from_a_boundary_equals_the_full_replay_for_every_later_game(
    games: list[GameRow],
) -> None:
    boundaries = season_boundaries(games)
    for season, ratings in boundaries.items():
        full, shard = EloBook(), EloBook(ratings)
        later = [g for g in games if g.season >= season]
        for game in games:
            if game.season >= season:
                break
            full.update(game)
        # The shard starts with the ratings before its first game, as the full replay has them.
        assert full.ratings() == shard.ratings()
        for game in later:
            assert full.home_win_chance(game.home, game.away) == shard.home_win_chance(
                game.home, game.away
            )
            full.update(game)
            shard.update(game)
        assert full.ratings() == shard.ratings()


@given(_histories())
def test_the_boundary_of_the_first_season_is_empty_and_the_last_matches_the_replay(
    games: list[GameRow],
) -> None:
    boundaries = season_boundaries(games)
    assert boundaries[1] == {}
    last = max(boundaries)
    before_last = replay([g for g in games if g.season < last])
    assert boundaries[last] == before_last.ratings()


def test_a_boundary_is_a_copy_that_later_updates_do_not_change() -> None:
    games = [_game(1, 1, "TOR", "BOS", 2), _game(2, 2, "TOR", "BOS", 1)]
    boundaries = season_boundaries(games)
    snapshot = dict(boundaries[2])
    book = EloBook(boundaries[2])
    book.update(games[1])
    assert boundaries[2] == snapshot


def test_a_book_keeps_no_reference_to_the_ratings_it_started_from() -> None:
    start = {"TOR": 1510.0}
    book = EloBook(start)
    book.update(_game(1, 1, "TOR", "BOS", 1))
    assert start == {"TOR": 1510.0}


def test_the_rows_of_a_season_from_its_boundary_equal_the_rows_of_the_full_build(
    store: sqlite3.Connection,
) -> None:
    seed(store, range(1, 8))
    store.commit()
    full = training_rows(store, 2, 7, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES)
    games = [row.game for row in training_rows(store, 1, 7, GoalieMode.ACTUAL, 1, names=FEATURES)]
    boundaries = season_boundaries(games)
    for season in (2, 5, 7):
        shard = training_rows(
            store,
            season,
            season,
            GoalieMode.ACTUAL,
            PLAN.burn_in,
            names=FEATURES,
            elo_start=boundaries[season],
        )
        assert shard == [row for row in full if row.game.season == season]


def test_the_sharded_build_equals_two_full_builds(store: sqlite3.Connection) -> None:
    seed(store, range(1, 8))
    store.commit()
    last = PLAN.scored[1]
    expected_actual = training_rows(
        store, PLAN.fit_first, last, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES
    )
    expected_guess = training_rows(
        store, PLAN.fit_first, last, GoalieMode.GUESS, PLAN.burn_in, names=FEATURES
    )
    actual, guess = build_sharded(store, PLAN, FEATURES, 3)
    assert actual == expected_actual
    assert guess == expected_guess


def test_the_sharded_build_of_an_empty_store_is_empty(store: sqlite3.Connection) -> None:
    store.commit()
    assert build_sharded(store, PLAN, FEATURES, 2) == ([], [])
