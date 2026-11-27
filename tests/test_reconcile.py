"""Tests for the pure reconciliation checks of the play-by-play and the shifts (#35)."""

import pytest

from hockey_model.reconcile import (
    CheckResult,
    SituationEvent,
    check_espn_teams,
    check_goals_by_player,
    check_goals_by_team,
    check_roster,
    check_situation,
    notes_json,
    status_of,
)
from hockey_model.stints import Stint

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
    assert check_roster([1, 2, 2], {1, 2, 3}).passed
    result = check_roster([1, 2, 9, 7], {1, 2})
    assert not result.passed
    assert result.detail == {"missing": [7, 9]}


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


def test_an_event_with_no_code_is_not_checked() -> None:
    assert check_situation([SituationEvent(1, 1, 50, None)], [_stint(0, 100, 5, 5)]).passed


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
        ((), True, "reconciled"),
    ],
)
def test_the_status_follows_the_checks_and_the_last_revisit(
    passes: tuple[bool, ...], settled: bool, status: str
) -> None:
    results = [_result(f"c{i}", p) for i, p in enumerate(passes)]
    assert status_of(results, settled=settled) == status


def test_the_notes_hold_the_result_of_every_check_and_the_dropped_shift_count() -> None:
    text = notes_json([_result("a", True), CheckResult("b", False, {"x": 1})], dropped_shifts=2)
    assert text == (
        '[{"check":"a","passed":true,"detail":{}},'
        '{"check":"b","passed":false,"detail":{"x":1}},'
        '{"check":"dropped_shifts","passed":true,"detail":{"count":2}}]'
    )
