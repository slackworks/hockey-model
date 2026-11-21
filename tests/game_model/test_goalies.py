"""Tests for goalie resolution."""

import sqlite3
from datetime import timedelta

import pytest
from seeding import add_game, add_goalie, add_injury, add_starter_read, utc

from hockey_model.game import (
    Game,
    GoalieStart,
    MissingKind,
    StarterSource,
    StarterStatus,
)
from hockey_model.game_model.goalies import STARTER_WINDOW, resolve_goalie
from hockey_model.types import Player, Slot

START = utc("2026-01-20T00:00")
AS_OF = utc("2026-01-19T16:00")


@pytest.fixture
def season(store: sqlite3.Connection) -> sqlite3.Connection:
    """Ten TOR games: goalie 11 (Connor Ingram) started 7 of them, 12 (Joseph Woll) 3."""
    for number in range(1, 11):
        add_game(store, number, f"2026-01-{number:02d}T00:00")
        add_goalie(
            store,
            number,
            "TOR",
            11 if number % 10 > 3 or number == 10 else 12,
            "C. Ingram" if number % 10 > 3 or number == 10 else "J. Woll",
        )
    return store


def game() -> Game:
    return Game(100, "TOR", "BOS", START)


def names(start: GoalieStart | None) -> str | None:
    return None if start is None else start.player.full_name


def kinds(missing) -> list[MissingKind]:
    return [m.kind for m in missing]


def test_game_goalie_wins(season: sqlite3.Connection) -> None:
    player = Player("x", None, None, "Given Goalie", frozenset({Slot.G}), "TOR")
    given = GoalieStart(
        player, StarterStatus.CONFIRMED, StarterSource.ESPN, AS_OF - timedelta(hours=1)
    )
    chosen = Game(100, "TOR", "BOS", START, home_goalie=given)
    start, missing = resolve_goalie(season, chosen, "TOR", AS_OF, use_reads=True)
    assert start is given
    assert missing == ()


def test_resolve_first_confirmed_wins(season: sqlite3.Connection) -> None:
    add_starter_read(
        season, 100, "TOR", "espn", "Joseph Woll", "confirmed", fetched_at="2026-01-19T15:00"
    )
    add_starter_read(
        season,
        100,
        "TOR",
        "daily_faceoff",
        "Connor Ingram",
        "confirmed",
        fetched_at="2026-01-19T14:00",
    )
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert start is not None
    assert names(start) == "C. Ingram"
    assert (start.source, start.status) == (StarterSource.DAILY_FACEOFF, StarterStatus.CONFIRMED)
    assert kinds(missing) == [MissingKind.GOALIE_CONFLICT]
    note = missing[0].note
    assert missing[0].team == "TOR"
    assert "C. Ingram was confirmed first (daily_faceoff, 2026-01-19T14:00" in note
    assert "J. Woll was confirmed later (espn, 2026-01-19T15:00" in note


def test_a_second_confirmation_of_the_same_goalie_is_not_a_conflict(
    season: sqlite3.Connection,
) -> None:
    add_starter_read(
        season, 100, "TOR", "espn", "Connor Ingram", "confirmed", fetched_at="2026-01-19T14:00"
    )
    add_starter_read(
        season,
        100,
        "TOR",
        "daily_faceoff",
        "C. Ingram",
        "confirmed",
        fetched_at="2026-01-19T15:00",
    )
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert names(start) == "C. Ingram"
    assert missing == ()


def test_resolve_ignores_a_read_fetched_after_as_of(season: sqlite3.Connection) -> None:
    add_starter_read(
        season, 100, "TOR", "espn", "Joseph Woll", "confirmed", fetched_at="2026-01-19T17:00"
    )
    start, _ = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert start is not None
    assert start.source is StarterSource.MODEL_GUESS


def test_resolve_likely_reports_unconfirmed(season: sqlite3.Connection) -> None:
    add_starter_read(
        season, 100, "TOR", "espn", "Joseph Woll", "likely", fetched_at="2026-01-19T12:00"
    )
    add_starter_read(
        season,
        100,
        "TOR",
        "daily_faceoff",
        "Connor Ingram",
        "likely",
        fetched_at="2026-01-19T13:00",
    )
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert start is not None
    assert names(start) == "C. Ingram"
    assert start.status is StarterStatus.LIKELY
    assert kinds(missing) == [MissingKind.GOALIE_CONFLICT, MissingKind.GOALIE_UNCONFIRMED]
    assert "J. Woll was likely (espn, 2026-01-19T12:00" in missing[0].note
    assert "C. Ingram is likely later (daily_faceoff, 2026-01-19T13:00" in missing[0].note


def test_two_likely_reads_of_the_same_goalie_are_not_a_conflict(season: sqlite3.Connection) -> None:
    for source, stamp in (("espn", "2026-01-19T12:00"), ("daily_faceoff", "2026-01-19T13:00")):
        add_starter_read(season, 100, "TOR", source, "Connor Ingram", "likely", fetched_at=stamp)
    _, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert kinds(missing) == [MissingKind.GOALIE_UNCONFIRMED]


def test_a_conflict_between_likely_reads_is_hidden_by_a_confirmed_read(
    season: sqlite3.Connection,
) -> None:
    add_starter_read(
        season, 100, "TOR", "espn", "Joseph Woll", "likely", fetched_at="2026-01-19T11:00"
    )
    add_starter_read(
        season, 100, "TOR", "espn", "Connor Ingram", "likely", fetched_at="2026-01-19T12:00"
    )
    add_starter_read(
        season, 100, "TOR", "espn", "Connor Ingram", "confirmed", fetched_at="2026-01-19T13:00"
    )
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert start is not None
    assert start.status is StarterStatus.CONFIRMED
    assert missing == ()


def test_resolve_confirmed_beats_a_newer_likely(season: sqlite3.Connection) -> None:
    add_starter_read(
        season, 100, "TOR", "espn", "Joseph Woll", "confirmed", fetched_at="2026-01-19T12:00"
    )
    add_starter_read(
        season,
        100,
        "TOR",
        "daily_faceoff",
        "Connor Ingram",
        "likely",
        fetched_at="2026-01-19T13:00",
    )
    start, _ = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert names(start) == "J. Woll"


def test_resolve_guess_uses_most_starts(season: sqlite3.Connection) -> None:
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert start is not None
    assert names(start) == "C. Ingram"
    assert (start.status, start.source) == (StarterStatus.UNKNOWN, StarterSource.MODEL_GUESS)
    assert kinds(missing) == [MissingKind.GOALIE_UNKNOWN]


def test_resolve_guess_skips_injured_goalie(season: sqlite3.Connection) -> None:
    add_injury(season, "TOR", "Connor Ingram", "Injured Reserve", "2026-01-19T10:00")
    start, _ = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert names(start) == "J. Woll"


def test_resolve_guess_keeps_a_day_to_day_goalie(season: sqlite3.Connection) -> None:
    add_injury(season, "TOR", "Connor Ingram", "Day-To-Day", "2026-01-19T10:00")
    start, _ = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert names(start) == "C. Ingram"


def test_resolve_guess_counts_only_the_window(store: sqlite3.Connection) -> None:
    for number in range(1, STARTER_WINDOW + 4):
        add_game(store, number, f"2026-01-{number:02d}T00:00")
        old = number <= 8
        add_goalie(store, number, "TOR", 11 if old else 12, "C. Ingram" if old else "J. Woll")
    start, _ = resolve_goalie(store, game(), "TOR", AS_OF, use_reads=False)
    assert names(start) == "J. Woll"


def test_resolve_unknown_name_reports_name(season: sqlite3.Connection) -> None:
    add_starter_read(
        season, 100, "TOR", "espn", "Nobody Here", "confirmed", fetched_at="2026-01-19T14:00"
    )
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert names(start) == "C. Ingram"
    assert kinds(missing) == [MissingKind.GOALIE_UNKNOWN, MissingKind.GOALIE_UNKNOWN]
    assert "Nobody Here" in missing[0].note


def test_resolve_two_matching_goalies_is_unknown(season: sqlite3.Connection) -> None:
    add_game(season, 50, "2026-01-11T00:00")
    add_goalie(season, 50, "TOR", 13, "Cole Ingram")
    add_starter_read(
        season, 100, "TOR", "espn", "Connor Ingram", "confirmed", fetched_at="2026-01-19T14:00"
    )
    _, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert "matches 2 goalies" in missing[0].note


def test_resolve_finds_a_traded_goalie_in_the_league(season: sqlite3.Connection) -> None:
    add_game(season, 60, "2026-01-12T00:00", home="MTL", away="NYR")
    add_goalie(season, 60, "MTL", 31, "S. Montembeault")
    add_starter_read(
        season,
        100,
        "TOR",
        "espn",
        "Samuel Montembeault",
        "confirmed",
        fetched_at="2026-01-19T14:00",
    )
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert names(start) == "S. Montembeault"
    assert missing == ()


def test_resolve_late_run_uses_boxscore(season: sqlite3.Connection) -> None:
    add_game(season, 100, "2026-01-20T00:00", fetched_at=utc("2026-01-20T04:00"))
    add_goalie(season, 100, "TOR", 12, "J. Woll", fetched_at=utc("2026-01-20T04:00"))
    add_starter_read(
        season, 100, "TOR", "espn", "Connor Ingram", "confirmed", fetched_at="2026-01-19T14:00"
    )
    late = START + timedelta(hours=1)
    start, missing = resolve_goalie(season, game(), "TOR", late, use_reads=True)
    assert start is not None
    assert names(start) == "J. Woll"
    assert start.source is StarterSource.NHL_BOXSCORE
    assert kinds(missing) == [MissingKind.LATE_RUN]


def test_resolve_late_run_without_boxscore_falls_back(season: sqlite3.Connection) -> None:
    late = START + timedelta(hours=1)
    start, missing = resolve_goalie(season, game(), "TOR", late, use_reads=True)
    assert start is not None
    assert start.source is StarterSource.MODEL_GUESS
    assert kinds(missing) == [MissingKind.GOALIE_UNKNOWN]


def test_resolve_before_start_ignores_the_boxscore(season: sqlite3.Connection) -> None:
    add_game(season, 100, "2026-01-20T00:00")
    add_goalie(season, 100, "TOR", 12, "J. Woll")
    start, _ = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert start is not None
    assert start.source is StarterSource.MODEL_GUESS


def test_resolve_without_reads_ignores_reads(season: sqlite3.Connection) -> None:
    add_starter_read(
        season, 100, "TOR", "espn", "Joseph Woll", "confirmed", fetched_at="2026-01-19T14:00"
    )
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=False)
    assert names(start) == "C. Ingram"
    assert kinds(missing) == [MissingKind.GOALIE_UNKNOWN]


def test_resolve_with_no_history_returns_none(store: sqlite3.Connection) -> None:
    start, missing = resolve_goalie(store, game(), "TOR", AS_OF, use_reads=True)
    assert start is None
    assert kinds(missing) == [MissingKind.GOALIE_UNKNOWN]
    assert "no goalie start" in missing[0].note


def test_resolve_skips_a_read_with_unknown_status(season: sqlite3.Connection) -> None:
    add_starter_read(
        season, 100, "TOR", "daily_faceoff", "Joseph Woll", "unknown", fetched_at="2026-01-19T14:00"
    )
    start, _ = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert start is not None
    assert start.source is StarterSource.MODEL_GUESS


def test_an_injury_on_another_team_does_not_remove_the_goalie(season: sqlite3.Connection) -> None:
    add_injury(season, "MTL", "Connor Ingram", "Out", "2026-01-19T10:00")
    start, _ = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert start is not None
    assert names(start) == "C. Ingram"


def test_a_one_word_name_says_why_it_does_not_match(season: sqlite3.Connection) -> None:
    add_starter_read(
        season, 100, "TOR", "espn", "Ingram", "confirmed", fetched_at="2026-01-19T14:00"
    )
    _, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=True)
    assert "no first name or initial" in missing[0].note


def test_an_ambiguous_injured_name_is_noted(season: sqlite3.Connection) -> None:
    add_game(season, 50, "2026-01-11T00:00")
    add_goalie(season, 50, "TOR", 13, "Cole Ingram")
    add_injury(season, "TOR", "Connor Ingram", "Out", "2026-01-19T10:00")
    _, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=False)
    assert any("an injured player is unclear" in m.note for m in missing)


def test_a_skater_injury_does_not_match_goalies_of_other_teams(season: sqlite3.Connection) -> None:
    add_game(season, 60, "2026-01-12T00:00")
    add_goalie(season, 60, "BOS", 31, "Sam Smith")
    add_goalie(season, 60, "BOS", 32, "Sean Smith")
    add_injury(season, "TOR", "Sam Smith", "Out", "2026-01-19T10:00")
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=False)
    assert names(start) == "C. Ingram"
    assert not any("an injured player is unclear" in m.note for m in missing)


def test_a_stale_injury_list_adds_a_note_to_the_guess(season: sqlite3.Connection) -> None:
    add_injury(season, "TOR", "C. Ingram", "Out", "2026-01-10T00:00")
    season.commit()
    start, missing = resolve_goalie(season, game(), "TOR", AS_OF, use_reads=False)
    # The old list is not used, so the guess is still Ingram, and the note says why.
    assert start is not None and start.player.full_name == "C. Ingram"
    assert any("injury list" in m.note for m in missing)
