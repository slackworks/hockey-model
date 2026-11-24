"""Tests for the as-of reads."""

import sqlite3
from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from seeding import (
    FETCHED,
    add_game,
    add_goalie,
    add_goalie_form,
    add_injury,
    add_starter_read,
    add_team_form,
    fresh_store,
    record_injury_snapshot,
    utc,
)

from hockey_model.sources.moneypuck import TeamGameRow
from hockey_model.store.moneypuck_writes import write_team_games
from hockey_model.store.reads import (
    RESULT_DELAY,
    final_games_before,
    franchise_codes,
    goalie_starts_before,
    injury_reads_before,
    mp_goalie_form_before,
    mp_team_form_before,
    player_catalog,
    starter_reads_before,
    team_games_before,
)
from hockey_model.types import HockeyModelError, Slot


def ids(games) -> list[int]:
    return [g.game_id for g in games]


def test_reads_hide_game_at_as_of(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-10T00:00")
    assert ids(final_games_before(store, utc("2026-01-10T00:00"))) == []
    assert ids(final_games_before(store, utc("2026-01-10T01:00"))) == []


def test_result_is_known_after_the_delay(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-10T00:00")
    edge = utc("2026-01-10T00:00") + RESULT_DELAY
    assert ids(final_games_before(store, edge)) == [1]
    assert ids(final_games_before(store, edge - timedelta(seconds=1))) == []


def test_reads_late_west_coast_game(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-10T03:30")
    add_game(store, 2, "2026-01-10T00:00")
    assert ids(final_games_before(store, utc("2026-01-10T03:30"))) == [2]


def test_reads_do_not_filter_results_by_fetch_time(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2016-01-10T00:00")
    assert ids(final_games_before(store, utc("2016-02-01T00:00"))) == [1]


def test_reads_hide_a_game_that_is_not_final(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-10T00:00", state="LIVE")
    add_game(store, 2, "2026-01-11T00:00", state="FUT")
    assert final_games_before(store, utc("2026-02-01T00:00")) == []


def test_reads_use_the_final_read_of_a_game(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-10T00:00", state="LIVE", fetched_at=utc("2026-01-10T01:00"))
    add_game(store, 1, "2026-01-10T00:00", fetched_at=utc("2026-01-10T04:00"))
    games = final_games_before(store, utc("2026-02-01T00:00"))
    assert [(g.game_id, g.home_score, g.state) for g in games] == [(1, 3, "OFF")]


def test_final_games_are_oldest_first(store: sqlite3.Connection) -> None:
    add_game(store, 2, "2026-01-12T00:00")
    add_game(store, 1, "2026-01-10T00:00")
    assert ids(final_games_before(store, utc("2026-02-01T00:00"))) == [1, 2]


def test_team_games_follow_franchise(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2024-01-10T00:00", home="ARI", away="BOS")
    add_game(store, 2, "2025-01-10T00:00", home="TOR", away="UTA")
    before = utc("2026-01-01T00:00")
    assert ids(team_games_before(store, "UTA", before, 10)) == [2, 1]
    assert ids(team_games_before(store, "ARI", before, 10)) == [2, 1]
    assert franchise_codes("UTA") == ("ARI", "UTA")
    assert franchise_codes("TOR") == ("TOR",)


def test_team_games_are_newest_first_and_limited(store: sqlite3.Connection) -> None:
    for number in range(1, 6):
        add_game(store, number, f"2026-01-0{number}T00:00")
    games = team_games_before(store, "TOR", utc("2026-02-01T00:00"), 3)
    assert ids(games) == [5, 4, 3]


def test_goalie_starts_only_the_starter(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-01T00:00")
    add_goalie(store, 1, "TOR", 11, "A Goalie")
    add_goalie(store, 1, "TOR", 12, "B Goalie", started=False)
    add_goalie(store, 1, "BOS", 21, "C Goalie")
    assert goalie_starts_before(store, "TOR", utc("2026-02-01T00:00"), 10) == [(1, 11)]


def test_goalie_starts_hide_recent_game(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-01T00:00")
    add_goalie(store, 1, "TOR", 11, "A Goalie")
    assert goalie_starts_before(store, "TOR", utc("2026-01-01T02:00"), 10) == []


def test_mp_team_form_is_newest_first_and_hides_recent(store: sqlite3.Connection) -> None:
    for number in (1, 2, 3):
        add_game(store, number, f"2026-01-0{number}T00:00")
        add_team_form(store, number, "TOR", float(number), 0.5)
    rows = mp_team_form_before(store, "TOR", utc("2026-01-03T00:00"), 10)
    assert rows == [(2.0, 0.5), (1.0, 0.5)]
    assert mp_team_form_before(store, "TOR", utc("2026-02-01T00:00"), 1) == [(3.0, 0.5)]


def test_mp_team_form_follows_franchise(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2024-01-01T00:00", home="ARI", away="BOS")
    add_team_form(store, 1, "ARI", 2.5, 1.5)
    assert mp_team_form_before(store, "UTA", utc("2026-01-01T00:00"), 5) == [(2.5, 1.5)]


def test_mp_team_form_ignores_a_row_without_a_stored_game(store: sqlite3.Connection) -> None:
    add_team_form(store, 99, "TOR", 2.5, 1.5)
    assert mp_team_form_before(store, "TOR", utc("2026-01-01T00:00"), 5) == []


def test_mp_goalie_form(store: sqlite3.Connection) -> None:
    for number in (1, 2):
        add_game(store, number, f"2026-01-0{number}T00:00")
        add_goalie_form(store, number, 11, 2.0 + number, number)
    rows = mp_goalie_form_before(store, 11, utc("2026-02-01T00:00"), 10)
    assert rows == [(4.0, 2), (3.0, 1)]
    assert mp_goalie_form_before(store, 11, utc("2026-01-01T02:00"), 10) == []
    assert mp_goalie_form_before(store, 12, utc("2026-02-01T00:00"), 10) == []


def test_starter_reads_hide_rows_fetched_after_as_of(store: sqlite3.Connection) -> None:
    add_starter_read(store, 1, "TOR", "espn", "B", "likely", fetched_at="2026-01-10T15:00")
    add_starter_read(
        store, 1, "TOR", "daily_faceoff", "A", "confirmed", fetched_at="2026-01-10T14:00"
    )
    add_starter_read(store, 1, "TOR", "espn", "C", "confirmed", fetched_at="2026-01-10T20:00")
    add_starter_read(store, 1, "BOS", "espn", "D", "confirmed", fetched_at="2026-01-10T14:00")
    reads = starter_reads_before(store, 1, "TOR", utc("2026-01-10T16:00"))
    assert [(r.goalie_name, r.source.value, r.status.value) for r in reads] == [
        ("A", "daily_faceoff", "confirmed"),
        ("B", "espn", "likely"),
    ]
    assert reads[0].fetched_at == utc("2026-01-10T14:00")


def test_injury_reads_use_the_newest_list_before_as_of(store: sqlite3.Connection) -> None:
    add_injury(store, "TOR", "Old Name", "Out", "2026-01-01T00:00")
    add_injury(store, "TOR", "New Name", "Out", "2026-01-05T00:00")
    add_injury(store, "BOS", "Other", "Day-To-Day", "2026-01-05T00:00")
    add_injury(store, "TOR", "Future", "Out", "2026-01-09T00:00")
    names = {i.player_name for i in injury_reads_before(store, utc("2026-01-05T12:00"))}
    assert names == {"New Name", "Other"}
    assert injury_reads_before(store, utc("2025-12-01T00:00")) == []


def test_player_catalog_uses_newest_row_and_maps_positions(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-01T00:00")
    add_game(store, 2, "2026-01-05T00:00")
    add_goalie(store, 1, "TOR", 11, "A. Goalie", fetched_at=utc("2026-01-02T00:00"))
    add_goalie(store, 2, "BOS", 11, "A. Goalie", fetched_at=utc("2026-01-06T00:00"))
    catalog = player_catalog(store)
    player = catalog.resolve(11)
    assert (player.team_code, player.canonical_id) == ("BOS", "nhl:11")
    assert player.eligible == frozenset({Slot.G})
    assert len(catalog.players) == 1


def test_an_empty_injury_list_clears_the_earlier_one(store: sqlite3.Connection) -> None:
    add_injury(store, "TOR", "Hurt Player", "Out", "2026-01-01T00:00")
    record_injury_snapshot(store, "2026-01-05T00:00")
    assert injury_reads_before(store, utc("2026-01-05T12:00")) == []
    assert [i.player_name for i in injury_reads_before(store, utc("2026-01-01T12:00"))] == [
        "Hurt Player"
    ]


def test_a_stored_team_game_without_expected_goals_fails_the_read_and_names_the_game(
    store: sqlite3.Connection,
) -> None:
    add_game(store, 1, "2026-01-01T00:00")
    add_team_form(store, 1, "TOR", 1.0, 0.5)
    store.execute("UPDATE mp_team_games SET xg_against = NULL WHERE game_id = 1")
    with pytest.raises(HockeyModelError, match="team expected goals of TOR: game 1 has no value"):
        mp_team_form_before(store, "TOR", utc("2026-02-01T00:00"), 5)


def test_a_stored_goalie_game_without_expected_goals_fails_the_read_and_names_the_game(
    store: sqlite3.Connection,
) -> None:
    add_game(store, 2, "2026-01-02T00:00")
    add_goalie_form(store, 2, 11, 2.0, 1)
    store.execute("UPDATE mp_goalie_games SET xg_against = NULL WHERE game_id = 2")
    with pytest.raises(HockeyModelError, match="goalie expected goals of 11: game 2 has no value"):
        mp_goalie_form_before(store, 11, utc("2026-02-01T00:00"), 5)


def test_a_null_row_outside_the_window_does_not_fail_the_read(store: sqlite3.Connection) -> None:
    for number in (1, 2):
        add_game(store, number, f"2026-01-0{number}T00:00")
        add_team_form(store, number, "TOR", float(number), 0.5)
    store.execute("UPDATE mp_team_games SET xg_for = NULL WHERE game_id = 1")
    assert mp_team_form_before(store, "TOR", utc("2026-02-01T00:00"), 1) == [(2.0, 0.5)]


_DAY0 = datetime(2026, 1, 1, tzinfo=FETCHED.tzinfo)


@settings(max_examples=50, deadline=None)
@given(
    start_days=st.lists(
        st.integers(min_value=1, max_value=30), min_size=1, max_size=8, unique=True
    ),
    forms=st.lists(
        st.lists(st.sampled_from([0.5, 1.0, 2.5, 3.0]), max_size=2), min_size=8, max_size=8
    ),
    cutoff_day=st.integers(min_value=0, max_value=31),
    limit=st.integers(min_value=1, max_value=10),
)
def test_mp_team_form_is_the_newest_read_of_the_newest_games_that_were_over(
    tmp_path_factory: pytest.TempPathFactory,
    start_days: list[int],
    forms: list[list[float]],
    cutoff_day: int,
    limit: int,
) -> None:
    """Game numbers run against start order, so the read must sort by start, not by number.

    A game keeps its form in a list of reads, oldest first. An empty list means no stored row.
    """
    expected: list[tuple[int, float]] = []
    with fresh_store(tmp_path_factory.mktemp("form")) as store:
        for number, (day, reads) in enumerate(zip(start_days, forms, strict=False), start=1):
            add_game(store, number, f"2026-01-{day:02d}T00:00")
            for age, xg_for in enumerate(reads):
                row = TeamGameRow(number, "TOR", "2026-01-01", xg_for, 0.5, 50, 50)
                write_team_games(store, [row], FETCHED + timedelta(days=age))
            if reads and _DAY0 + timedelta(days=day - 1) <= _DAY0 + timedelta(days=cutoff_day):
                expected.append((day, reads[-1]))
        before = _DAY0 + timedelta(days=cutoff_day) + RESULT_DELAY
        found = mp_team_form_before(store, "TOR", before, limit)
    newest_first = sorted(expected, reverse=True)[:limit]
    assert found == [(xg_for, 0.5) for _, xg_for in newest_first]
