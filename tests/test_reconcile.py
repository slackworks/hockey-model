"""Tests for the pure reconciliation checks of the play-by-play and the shifts (#35)."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.reconcile import (
    CheckResult,
    SituationEvent,
    check_espn_teams,
    check_game_state,
    check_goals_by_player,
    check_goals_by_team,
    check_roster,
    check_shift_lengths,
    check_situation,
    notes_json,
    status_of,
)
from hockey_model.stints import Stint
from hockey_model.types import HockeyModelError

HOME = "EDM"
AWAY = "LAK"


def test_goals_by_team_equal_the_final_score() -> None:
    result = check_goals_by_team({HOME: 4, AWAY: 3}, HOME, AWAY, (4, 3), "REG")
    assert result.passed
    assert result.name == "goals_by_team"


def test_a_shootout_winner_has_one_goal_less_in_the_play_by_play() -> None:
    assert check_goals_by_team({HOME: 3, AWAY: 2}, HOME, AWAY, (3, 2), "OT").passed
    won = check_goals_by_team({HOME: 3, AWAY: 3}, HOME, AWAY, (4, 3), "SO")
    assert won.passed
    lost = check_goals_by_team({HOME: 3, AWAY: 3}, HOME, AWAY, (3, 4), "SO")
    assert lost.passed


def test_a_missing_goal_is_a_failure_that_names_both_counts() -> None:
    result = check_goals_by_team({HOME: 3, AWAY: 3}, HOME, AWAY, (4, 3), "REG")
    assert not result.passed
    assert result.detail == {"expected": {HOME: 4, AWAY: 3}, "found": {HOME: 3, AWAY: 3}}


@pytest.mark.parametrize("scores", [(None, 3), (4, None), (None, None)])
def test_a_game_with_no_final_score_fails_the_goal_check(
    scores: tuple[int | None, int | None],
) -> None:
    result = check_goals_by_team({HOME: 4, AWAY: 3}, HOME, AWAY, scores, "REG")
    assert not result.passed
    assert result.detail == {"reason": "the game has no final score"}


def test_a_shootout_with_equal_scores_fails_the_goal_check() -> None:
    result = check_goals_by_team({HOME: 3, AWAY: 3}, HOME, AWAY, (3, 3), "SO")
    assert not result.passed


def test_goals_by_player_equal_the_boxscore() -> None:
    assert check_goals_by_player({1: 2, 2: 1}, {1: 2, 2: 1}).passed


def test_a_player_with_a_different_goal_count_is_named() -> None:
    result = check_goals_by_player({1: 2, 2: 1}, {1: 2, 3: 1})
    assert not result.passed
    assert result.detail["total"] == 2
    assert result.detail["players"] == [
        {"player": 2, "play_by_play": 1, "boxscore": 0},
        {"player": 3, "play_by_play": 0, "boxscore": 1},
    ]


def test_only_the_first_ten_players_are_listed_and_the_total_is_kept() -> None:
    result = check_goals_by_player({p: 1 for p in range(1, 16)}, {})
    assert result.detail["total"] == 15
    players = result.detail["players"]
    assert isinstance(players, list)
    assert len(players) == 10


def test_a_shift_player_that_the_roster_lacks_fails_the_roster_check() -> None:
    positions = {1: "C", 2: "D", 3: "G"}
    assert check_roster([1, 2, 2], positions).passed
    result = check_roster([1, 2, 9, 7], positions)
    assert not result.passed
    assert result.detail == {"missing": [7, 9], "unknown_position": []}


@pytest.mark.parametrize("position", ["", "g", "GOALIE", "X", "F"])
def test_a_roster_position_that_is_not_a_known_code_fails_the_roster_check(position: str) -> None:
    result = check_roster([1, 2], {1: "C", 2: position})
    assert not result.passed
    assert result.detail == {
        "missing": [],
        "unknown_position": [{"player": 2, "position": position}],
    }


@pytest.mark.parametrize("position", ["C", "L", "R", "D", "G"])
def test_each_position_code_of_the_feed_is_known(position: str) -> None:
    assert check_roster([1], {1: position}).passed


def _stint(start: int, end: int, home: int, away: int) -> Stint:
    return Stint(1, start, end, tuple(range(home)), tuple(range(10, 10 + away)), (90,), (91,))


def test_an_event_with_enough_skaters_in_its_stint_passes() -> None:
    stints = [_stint(0, 100, 5, 5)]
    assert check_situation([SituationEvent(1, 1, 50, "1551")], stints).passed


def test_an_event_with_fewer_skaters_than_the_code_fails_and_adds_no_player() -> None:
    stints = [_stint(0, 100, 5, 4)]
    result = check_situation([SituationEvent(1, 1, 50, "1551")], stints)
    assert not result.passed
    assert result.detail["total"] == 1
    assert result.detail["events"] == [
        {"event": 1, "expected": {"home": 5, "away": 5}, "found": {"home": 5, "away": 4}}
    ]


def test_a_stint_with_more_skaters_than_the_code_passes() -> None:
    stints = [_stint(0, 100, 6, 5)]
    assert check_situation([SituationEvent(1, 1, 50, "1551")], stints).passed


def test_a_faceoff_at_a_line_change_may_match_the_stint_after_the_change() -> None:
    stints = [_stint(0, 60, 5, 3), _stint(60, 100, 5, 4)]
    assert check_situation([SituationEvent(1, 1, 60, "1451")], stints).passed


def test_an_event_in_no_stint_fails_the_situation_check() -> None:
    result = check_situation([SituationEvent(7, 1, 500, "1551")], [_stint(0, 100, 5, 5)])
    assert not result.passed
    assert result.detail["events"] == [{"event": 7, "reason": "no stint holds the event"}]


def test_a_game_where_no_event_has_a_code_fails_because_nothing_was_checked() -> None:
    events = [SituationEvent(1, 1, 50, None), SituationEvent(2, 1, 60, None)]
    result = check_situation(events, [_stint(0, 100, 5, 5)])
    assert not result.passed
    assert result.detail == {"reason": "no event carries a situation code", "events": 2}


def test_a_game_with_no_event_fails_the_situation_check() -> None:
    assert not check_situation([], [_stint(0, 100, 5, 5)]).passed


def test_an_event_with_no_code_is_skipped_and_the_others_are_counted() -> None:
    events = [SituationEvent(1, 1, 50, None), SituationEvent(2, 1, 60, "1551")]
    result = check_situation(events, [_stint(0, 100, 5, 5)])
    assert result.passed
    assert result.detail == {"checked": 1}


def test_the_espn_teams_must_be_the_teams_of_the_game() -> None:
    assert check_espn_teams({HOME, AWAY}, HOME, AWAY).passed
    result = check_espn_teams({HOME, "CGY"}, HOME, AWAY)
    assert not result.passed
    assert result.detail == {"espn": ["CGY", "EDM"], "game": ["EDM", "LAK"]}


def _result(name: str, passed: bool) -> CheckResult:
    return CheckResult(name, passed, {})


@pytest.mark.parametrize(
    ("passes", "settled", "status"),
    [
        ((True, True), False, "provisional"),
        ((True, False), False, "provisional"),
        ((True, True), True, "reconciled"),
        ((True, False), True, "incomplete"),
    ],
)
def test_the_status_follows_the_checks_and_the_last_revisit(
    passes: tuple[bool, ...], settled: bool, status: str
) -> None:
    results = [_result(f"c{i}", p) for i, p in enumerate(passes)]
    assert status_of(results, settled=settled) == status


def test_a_zero_length_shift_is_noted_and_a_negative_one_fails_the_check() -> None:
    ok = check_shift_lengths(zero_length=3, negative_length=0)
    assert ok.passed
    assert ok.detail == {"zero_length": 3, "negative_length": 0}
    bad = check_shift_lengths(zero_length=0, negative_length=1)
    assert not bad.passed
    assert bad.detail == {"zero_length": 0, "negative_length": 1}


def test_the_notes_hold_the_result_of_every_check() -> None:
    text = notes_json([_result("a", True), CheckResult("b", False, {"x": 1})])
    assert text == (
        '[{"check":"a","passed":true,"detail":{}},{"check":"b","passed":false,"detail":{"x":1}}]'
    )


def test_a_goal_with_no_team_is_counted_apart_and_fails_the_check() -> None:
    result = check_goals_by_team({HOME: 3, AWAY: 3, None: 1}, HOME, AWAY, (3, 3), "REG")
    assert not result.passed
    assert result.detail["found"] == {HOME: 3, AWAY: 3, None: 1}


@pytest.mark.parametrize("state", ["FINAL", "OFF"])
def test_a_final_game_state_passes(state: str) -> None:
    result = check_game_state({state})
    assert result.passed
    assert result.detail == {}


@pytest.mark.parametrize(
    "states", [{"LIVE"}, {"CRIT"}, {"PRE"}, {"FUT"}, {"OFF", "LIVE"}, set(), {""}]
)
def test_a_game_state_that_is_not_final_fails_the_check(states: set[str]) -> None:
    result = check_game_state(states)
    assert not result.passed
    assert result.detail == {"states": sorted(states)}


@pytest.mark.parametrize("settled", [True, False])
def test_a_status_with_no_check_result_is_an_error(settled: bool) -> None:
    with pytest.raises(HockeyModelError, match="no check result"):
        status_of([], settled=settled)


check_results = st.lists(
    st.builds(
        CheckResult,
        st.text(min_size=1, max_size=8),
        st.booleans(),
        st.dictionaries(st.text(max_size=4), st.integers(0, 99), max_size=3),
    ),
    min_size=1,
    max_size=6,
)


@given(check_results)
def test_the_notes_read_back_to_the_results_in_order(results: list[CheckResult]) -> None:
    entries = json.loads(notes_json(results))
    assert entries == [{"check": r.name, "passed": r.passed, "detail": r.detail} for r in results]


stint_counts = st.lists(st.tuples(st.integers(3, 6), st.integers(3, 6)), min_size=1, max_size=5)
situation_rows = st.lists(
    st.tuples(st.integers(0, 49), st.integers(3, 6), st.integers(3, 6)), max_size=8
)


def _stints_of(counts: list[tuple[int, int]], extra: int = 0) -> list[Stint]:
    return [
        Stint(
            1,
            10 * i,
            10 * i + 10,
            tuple(range(h + extra)),
            tuple(range(20, 20 + a + extra)),
            (),
            (),
        )
        for i, (h, a) in enumerate(counts)
    ]


@given(stint_counts, situation_rows)
def test_more_skaters_never_turn_a_passing_situation_check_into_a_failing_one(
    counts: list[tuple[int, int]], rows: list[tuple[int, int, int]]
) -> None:
    events = [SituationEvent(i, 1, s, f"1{a}{h}1") for i, (s, h, a) in enumerate(rows)]
    before = check_situation(events, _stints_of(counts))
    after = check_situation(events, _stints_of(counts, extra=1))
    if before.passed:
        assert after.passed
    if not before.passed and "total" in before.detail:
        total = before.detail["total"]
        assert isinstance(total, int)
        assert total <= len(events)


def test_a_shootout_with_equal_scores_names_the_reason() -> None:
    result = check_goals_by_team({HOME: 3, AWAY: 3}, HOME, AWAY, (3, 3), "SO")
    assert result.detail == {"reason": "a shootout game has equal scores"}


def test_the_shootout_goal_is_taken_from_the_winner_only() -> None:
    result = check_goals_by_team({HOME: 3, AWAY: 3}, HOME, AWAY, (4, 3), "SO")
    assert result.detail == {"expected": {HOME: 3, AWAY: 3}, "found": {HOME: 3, AWAY: 3}}
    result = check_goals_by_team({HOME: 3, AWAY: 3}, HOME, AWAY, (3, 4), "SO")
    assert result.detail == {"expected": {HOME: 3, AWAY: 3}, "found": {HOME: 3, AWAY: 3}}


@pytest.mark.parametrize(
    ("goals", "scores"),
    [({HOME: 3}, (3, 0)), ({AWAY: 2}, (0, 2)), ({}, (0, 0))],
)
def test_a_team_with_no_goal_in_the_play_by_play_has_zero_goals(
    goals: dict[str | None, int], scores: tuple[int, int]
) -> None:
    result = check_goals_by_team(goals, HOME, AWAY, scores, "REG")
    assert result.passed
    assert result.detail == {
        "expected": {HOME: scores[0], AWAY: scores[1]},
        "found": {HOME: scores[0], AWAY: scores[1]},
    }


def test_a_player_with_no_goal_in_one_source_has_zero_goals_there() -> None:
    assert check_goals_by_player({1: 1}, {1: 1, 2: 0}).passed
    assert check_goals_by_player({1: 1, 2: 0}, {1: 1}).passed
    assert check_goals_by_player({}, {}).detail == {}


def test_a_pass_of_each_check_has_an_empty_detail() -> None:
    assert check_goals_by_player({1: 1}, {1: 1}).detail == {}
    assert check_roster([1], {1: "C"}).detail == {}
    assert check_espn_teams({HOME, AWAY}, HOME, AWAY).detail == {}
    assert check_game_state({"OFF"}).detail == {}


def test_each_situation_result_carries_the_name_of_the_check() -> None:
    stints = [_stint(0, 100, 5, 5)]
    for events in (
        [SituationEvent(1, 1, 50, None)],
        [SituationEvent(1, 1, 50, "1551")],
        [SituationEvent(1, 1, 500, "1551")],
    ):
        assert check_situation(events, stints).name == "situation"


def test_the_empty_status_message_says_what_to_do() -> None:
    with pytest.raises(HockeyModelError) as error:
        status_of([], settled=True)
    assert str(error.value) == (
        "status_of got no check result, so no game can be called reconciled; "
        "run the reconciliation checks first"
    )


def test_each_result_of_a_check_says_passed_with_a_real_boolean() -> None:
    results = [
        check_goals_by_team({HOME: 1}, HOME, AWAY, (1, 0), "REG"),
        check_goals_by_player({1: 1}, {1: 1}),
        check_roster([1], {1: "C"}),
        check_situation([SituationEvent(1, 1, 50, None)], []),
        check_espn_teams({HOME}, HOME, AWAY),
    ]
    assert [type(r.passed) for r in results] == [bool] * 5
