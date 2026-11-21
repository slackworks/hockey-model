"""Season shards: the Elo from a season boundary and the sharded build equal the full replay."""

import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from seeding import add_game
from test_runner_store import PLAN, seed

from hockey_model.backtest import shards
from hockey_model.backtest.shards import actual_rows, build_sharded
from hockey_model.game_model.elo import EloBook, replay, season_boundaries
from hockey_model.game_model.features import FEATURES, GoalieMode
from hockey_model.game_model.training import training_rows
from hockey_model.sources.nhl import GameRow
from hockey_model.store.db import connect
from hockey_model.types import HockeyModelError

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


def test_the_rows_for_a_fit_equal_the_full_build_for_any_worker_count(
    store: sqlite3.Connection,
) -> None:
    seed(store, range(1, 8))
    store.commit()
    expected = training_rows(store, 2, 7, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES)
    for workers in (1, 2, 4):
        assert actual_rows(store, 2, 7, FEATURES, workers, PLAN.burn_in) == expected


def test_the_rows_for_a_fit_of_a_later_range_start_from_the_boundary(
    store: sqlite3.Connection,
) -> None:
    seed(store, range(1, 8))
    store.commit()
    expected = training_rows(store, 4, 6, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES)
    assert actual_rows(store, 4, 6, FEATURES, 3, PLAN.burn_in) == expected


def test_a_burn_in_season_keeps_the_earlier_games_out_of_the_ratings(
    store: sqlite3.Connection,
) -> None:
    seed(store, range(1, 8))
    store.commit()
    for burn_in in (1, 2, 3):
        expected = training_rows(store, burn_in + 1, 7, GoalieMode.ACTUAL, burn_in, names=FEATURES)
        assert actual_rows(store, burn_in + 1, 7, FEATURES, 2, burn_in) == expected


def test_a_range_that_starts_before_the_burn_in_is_refused_with_elo_start(
    store: sqlite3.Connection,
) -> None:
    with pytest.raises(ValueError, match="before the burn-in season 3"):
        training_rows(store, 2, 4, GoalieMode.ACTUAL, 3, names=FEATURES, elo_start={})


@pytest.mark.parametrize("workers", [0, -2])
def test_the_rows_for_a_fit_refuse_a_worker_count_below_one(
    store: sqlite3.Connection, workers: int
) -> None:
    with pytest.raises(HockeyModelError, match="workers must be at least 1"):
        actual_rows(store, 2, 7, FEATURES, workers, PLAN.burn_in)


def test_the_sharded_rows_need_a_store_file() -> None:
    memory = sqlite3.connect(":memory:")
    try:
        with pytest.raises(HockeyModelError, match="no file"):
            actual_rows(memory, 2, 7, FEATURES, 2, PLAN.burn_in)
    finally:
        memory.close()


def test_the_sharded_rows_refuse_a_connection_with_uncommitted_changes(
    store: sqlite3.Connection,
) -> None:
    seed(store, range(1, 3))
    with pytest.raises(HockeyModelError, match="uncommitted changes"):
        actual_rows(store, 2, 2, FEATURES, 2, PLAN.burn_in)


def test_a_worker_sees_that_the_store_changed_during_the_run(
    store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store, range(1, 8))
    store.commit()
    real = shards._final_games

    def parent_saw_fewer(conn: sqlite3.Connection, burn_in: int) -> list[GameRow]:
        return real(conn, burn_in)[:-1] if conn is store else real(conn, burn_in)

    monkeypatch.setattr(shards, "_final_games", parent_saw_fewer)
    with pytest.raises(HockeyModelError, match="the store changed during the run"):
        actual_rows(store, 6, 7, FEATURES, 1 + 1, PLAN.burn_in)


@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_histories(), st.integers(1, 2), st.data())
def test_the_sharded_rows_equal_the_full_build_for_generated_histories(
    games: list[GameRow], burn_in: int, data: st.DataObject
) -> None:
    seasons = sorted({g.season for g in games if g.season >= burn_in})
    if not seasons:
        return
    first = data.draw(st.sampled_from(seasons))
    last = data.draw(st.sampled_from([s for s in seasons if s >= first]))
    with tempfile.TemporaryDirectory() as raw:
        conn = connect(Path(raw) / "s.db")
        try:
            for game in games:
                add_game(
                    conn,
                    game.game_id,
                    game.start.strftime("%Y-%m-%dT%H:%M"),
                    game.home,
                    game.away,
                    season=game.season,
                    score=(game.home_score or 0, game.away_score or 0),
                )
            conn.commit()
            names = ("rest",)
            expected = training_rows(conn, first, last, GoalieMode.ACTUAL, burn_in, names=names)
            assert actual_rows(conn, first, last, names, 2, burn_in) == expected
            full = [
                row.game
                for row in training_rows(conn, burn_in, 9, GoalieMode.ACTUAL, burn_in, names=names)
            ]
            boundaries = season_boundaries(full)
            for season in seasons:
                shard = training_rows(
                    conn,
                    season,
                    season,
                    GoalieMode.GUESS,
                    burn_in,
                    names=names,
                    elo_start=boundaries[season],
                )
                whole = training_rows(conn, season, season, GoalieMode.GUESS, burn_in, names=names)
                assert shard == whole
        finally:
            conn.close()


def _shard(store: sqlite3.Connection, season: int, final_games: int | None = None) -> shards.Shard:
    games = shards._final_games(store, PLAN.burn_in)
    return shards.Shard(
        season,
        GoalieMode.ACTUAL,
        PLAN.burn_in,
        tuple(FEATURES),
        season_boundaries(games)[season],
        len(games) if final_games is None else final_games,
    )


def test_the_final_games_include_the_burn_in_season(store: sqlite3.Connection) -> None:
    seed(store, range(1, 8))
    store.commit()
    seasons = {game.season for game in shards._final_games(store, PLAN.burn_in)}
    assert min(seasons) == PLAN.burn_in


def test_a_worker_builds_the_rows_of_its_season(store: sqlite3.Connection) -> None:
    seed(store, range(1, 8))
    store.commit()
    full = training_rows(store, 2, 7, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES)
    for season in (2, 5):
        rows = shards.build_season_rows(shards.database_path(store), _shard(store, season))
        assert rows == [row for row in full if row.game.season == season]


def test_a_worker_closes_its_connection(
    store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store, range(1, 8))
    store.commit()
    opened: list[sqlite3.Connection] = []
    real = shards.open_read_only

    def spy(path: Path) -> sqlite3.Connection:
        opened.append(real(path))
        return opened[-1]

    monkeypatch.setattr(shards, "open_read_only", spy)
    shards.build_season_rows(shards.database_path(store), _shard(store, 3))
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_a_worker_that_sees_another_game_count_names_both_counts(
    store: sqlite3.Connection,
) -> None:
    seed(store, range(1, 8))
    store.commit()
    shard = _shard(store, 3)
    stale = _shard(store, 3, shard.final_games + 1)
    with pytest.raises(HockeyModelError) as caught:
        shards.build_season_rows(shards.database_path(store), stale)
    assert str(caught.value) == (
        f"backtest: the store changed during the run: the parent read {stale.final_games} "
        f"final games and a worker read {shard.final_games}; do not refresh "
        "the store while a backtest or a fit runs, then run it again"
    )


def test_each_worker_gets_the_ratings_before_its_season(
    store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store, range(1, 8))
    store.commit()
    seen: dict[int, object] = {}
    real = shards.training_rows

    def spy(
        conn: sqlite3.Connection,
        first: int,
        last: int,
        mode: GoalieMode,
        burn_in: int,
        **kwargs: Any,
    ):
        seen[first] = kwargs.get("elo_start")
        return real(conn, first, last, mode, burn_in, **kwargs)

    monkeypatch.setattr(shards, "training_rows", spy)
    shards._build(store, (2, 7), PLAN.burn_in, FEATURES, (GoalieMode.ACTUAL,), 1)
    boundaries = season_boundaries(shards._final_games(store, PLAN.burn_in))
    assert sorted(seen) == [2, 3, 4, 5, 6, 7]
    assert seen == {season: boundaries[season] for season in range(2, 8)}
    assert seen[3]


def test_a_worker_result_count_that_differs_from_the_shard_count_is_refused(
    store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store, range(1, 8))
    store.commit()
    monkeypatch.setattr(shards, "run_units", lambda units, workers, label: [])
    with pytest.raises(ValueError, match="shorter"):
        shards._build(store, (2, 7), PLAN.burn_in, FEATURES, (GoalieMode.ACTUAL,), 1)
