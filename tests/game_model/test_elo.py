"""Tests for the Elo."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from hockey_model.game_model.elo import (
    ELO_HOME_ADV,
    ELO_INITIAL,
    ELO_K,
    EloBook,
    elo_expected,
    replay,
    season_boundaries,
)
from hockey_model.sources.nhl import GameRow


def final(game_id: int, home: str, away: str, hs: int, aws: int) -> GameRow:
    start = datetime(2026, 1, game_id, tzinfo=UTC)
    return GameRow(game_id, 20252026, 2, start, home, away, "OFF", hs, aws, "REG")


def expected(home: float, away: float) -> float:
    return 1 / (1 + 10 ** ((away - home - 28.0) / 400))


def test_constants_match_sportsbettr_nhl() -> None:
    assert (ELO_INITIAL, ELO_K, ELO_HOME_ADV) == (1500.0, 8.0, 28.0)


def test_elo_matches_the_sportsbettr_formula() -> None:
    games = [
        final(1, "TOR", "BOS", 4, 1),
        final(2, "BOS", "TOR", 2, 3),
        final(3, "TOR", "BOS", 1, 0),
    ]
    tor, bos = 1500.0, 1500.0
    for home_is_tor, hs, aws in ((True, 4, 1), (False, 2, 3), (True, 1, 0)):
        home, away = (tor, bos) if home_is_tor else (bos, tor)
        e = expected(home, away)
        actual = 1.0 if hs > aws else 0.0
        delta = 8.0 * (actual - e) * math.log(1 + abs(hs - aws))
        if home_is_tor:
            tor, bos = tor + delta, bos - delta
        else:
            bos, tor = bos + delta, tor - delta
    book = replay(games)
    assert book.rating("TOR") == pytest.approx(tor, abs=1e-9)
    assert book.rating("BOS") == pytest.approx(bos, abs=1e-9)


def test_elo_hand_computed_first_game() -> None:
    book = replay([final(1, "TOR", "BOS", 4, 1)])
    e = 1 / (1 + 10 ** (-28 / 400))
    assert book.rating("TOR") == pytest.approx(1500 + 8 * (1 - e) * math.log(4), abs=1e-9)
    assert book.rating("BOS") == pytest.approx(1500 - 8 * (1 - e) * math.log(4), abs=1e-9)


def test_elo_is_zero_sum() -> None:
    book = replay([final(1, "TOR", "BOS", 4, 1), final(2, "MTL", "TOR", 0, 2)])
    total = sum(book.rating(t) for t in ("TOR", "BOS", "MTL"))
    assert total == pytest.approx(3 * 1500.0, abs=1e-9)


@pytest.mark.parametrize("state", ["FUT", "LIVE", "PPD"])
def test_elo_rejects_non_final_game(state: str) -> None:
    game = GameRow(
        1, 20252026, 2, datetime(2026, 1, 1, tzinfo=UTC), "TOR", "BOS", state, None, None, None
    )
    with pytest.raises(ValueError, match="not final"):
        EloBook().update(game)


def test_elo_rejects_a_tied_score() -> None:
    with pytest.raises(ValueError, match="tied"):
        EloBook().update(final(1, "TOR", "BOS", 2, 2))


def test_elo_follows_franchise() -> None:
    book = replay([final(1, "ARI", "BOS", 3, 1)])
    assert book.rating("UTA") == book.rating("ARI") > ELO_INITIAL
    book.update(final(2, "UTA", "BOS", 3, 1))
    assert book.rating("ARI") == book.rating("UTA")


def test_elo_new_team_starts_at_initial() -> None:
    book = replay([final(1, "TOR", "BOS", 3, 1)])
    assert book.rating("VGK") == ELO_INITIAL
    book.update(final(2, "VGK", "MTL", 1, 0))
    home_edge = expected(1500.0, 1500.0)
    assert book.rating("VGK") == pytest.approx(1500 + 8 * (1 - home_edge) * math.log(2))
    assert book.rating("MTL") == pytest.approx(1500 - 8 * (1 - home_edge) * math.log(2))


def test_home_win_chance_with_equal_ratings_favors_home() -> None:
    assert EloBook().home_win_chance("TOR", "BOS") > 0.5


@given(
    st.floats(min_value=1000, max_value=2000),
    st.floats(min_value=1000, max_value=2000),
)
def test_elo_expected_is_a_probability_with_a_home_edge(a: float, b: float) -> None:
    assert 0 < elo_expected(a, b) < 1
    assert elo_expected(a, b) + elo_expected(b, a) > 1.0


@given(st.integers(min_value=0, max_value=9), st.integers(min_value=0, max_value=9))
def test_replay_keeps_the_sum_of_ratings(hs: int, aws: int) -> None:
    assume(hs != aws)
    book = replay([final(1, "TOR", "BOS", hs, aws)])
    assert book.rating("TOR") + book.rating("BOS") == pytest.approx(3000.0, abs=1e-9)


@pytest.mark.parametrize(
    ("state", "home", "away"),
    [("LIVE", 3, 2), ("OFF", None, 2), ("OFF", 3, None), ("FUT", None, None)],
)
def test_elo_rejects_a_game_that_is_not_a_scored_final(
    state: str, home: int | None, away: int | None
) -> None:
    game = GameRow(
        1, 20252026, 2, datetime(2026, 1, 1, tzinfo=UTC), "TOR", "BOS", state, home, away, None
    )
    with pytest.raises(ValueError, match="not final"):
        EloBook().update(game)


def test_an_away_win_lowers_the_home_rating_by_the_margin() -> None:
    book = replay([final(1, "TOR", "BOS", 1, 4)])
    expected = 1 / (1 + 10 ** (-28 / 400))
    assert book.rating("TOR") == pytest.approx(1500 - 8 * expected * math.log(4))


def _decisive(game_id: int, season: int, home: str, away: str, margin: int) -> GameRow:
    start = datetime(2015, 10, 1, tzinfo=UTC) + timedelta(hours=game_id)
    return GameRow(game_id, season, 2, start, home, away, "OFF", 3 + margin, 3, "REG")


def test_the_first_boundary_is_empty_and_a_later_one_is_the_replay_before_its_season() -> None:
    games = [_decisive(1, 1, "TOR", "BOS", 2), _decisive(2, 1, "BOS", "TOR", 1)]
    games += [_decisive(3, 2, "TOR", "BOS", 1), _decisive(4, 3, "MTL", "TOR", 3)]
    boundaries = season_boundaries(games)
    assert boundaries[1] == {}
    assert boundaries[2] == replay(games[:2]).ratings()
    assert boundaries[3] == replay(games[:3]).ratings()
    assert list(boundaries) == [1, 2, 3]


def test_a_season_that_starts_after_a_later_season_is_refused() -> None:
    games = [_decisive(1, 2024, "TOR", "BOS", 1), _decisive(2, 2023, "TOR", "BOS", 1)]
    with pytest.raises(
        ValueError, match="game 2 of season 2023 starts after a game of season 2024"
    ):
        season_boundaries(games)


def test_a_book_started_from_ratings_keeps_its_own_copy() -> None:
    start = {"TOR": 1510.0}
    book = EloBook(start)
    book.update(_decisive(1, 1, "TOR", "BOS", 1))
    assert start == {"TOR": 1510.0}
    assert book.ratings() != start
