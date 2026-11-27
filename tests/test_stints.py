"""Tests for the stint builder: merge, split, and the rule that owns a boundary event."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.stints import Interval, build_stints, merge_intervals, stint_at

HOME = "EDM"
AWAY = "LAK"


def _iv(player: int, team: str, start: int, end: int, period: int = 1) -> Interval:
    return Interval(player, team, period, start, end)


def test_overlapping_and_touching_intervals_of_one_player_become_one() -> None:
    merged, dropped = merge_intervals(
        [_iv(1, HOME, 0, 40), _iv(1, HOME, 30, 60), _iv(1, HOME, 60, 90), _iv(1, HOME, 100, 120)]
    )
    assert [(i.start, i.end) for i in merged] == [(0, 90), (100, 120)]
    assert dropped == []


def test_an_interval_with_no_length_is_dropped_and_reported() -> None:
    merged, dropped = merge_intervals([_iv(1, HOME, 50, 50), _iv(2, HOME, 60, 40)])
    assert merged == []
    assert [i.player_id for i in dropped] == [1, 2]


def test_two_players_with_the_same_id_on_two_teams_stay_apart() -> None:
    merged, _ = merge_intervals([_iv(1, HOME, 0, 30), _iv(1, AWAY, 10, 40)])
    assert len(merged) == 2


def _line_change() -> list[Interval]:
    """Home skaters 1 and 2 play 0..100; skater 3 replaces skater 2 at 60. The goalies stay."""
    return [
        _iv(1, HOME, 0, 100),
        _iv(2, HOME, 0, 60),
        _iv(3, HOME, 60, 100),
        _iv(20, AWAY, 0, 100),
        _iv(90, HOME, 0, 100),
        _iv(91, AWAY, 0, 100),
    ]


def test_a_stint_splits_at_each_change_of_players() -> None:
    stints = build_stints(_line_change(), {90: "G", 91: "G"}, HOME)
    assert [(s.start, s.end, s.home_skaters) for s in stints] == [
        (0, 60, (1, 2)),
        (60, 100, (1, 3)),
    ]
    assert all(s.home_goalie == 90 and s.away_goalie == 91 for s in stints)
    assert all(s.away_skaters == (20,) for s in stints)


@pytest.mark.parametrize(
    ("second", "owner"),
    [(1, (1, 2)), (59, (1, 2)), (60, (1, 2)), (61, (1, 3)), (100, (1, 3)), (0, (1, 2))],
)
def test_an_event_at_the_end_second_of_a_shift_belongs_to_that_shift(
    second: int, owner: tuple[int, ...]
) -> None:
    """A goal at a line change goes to the players who were on the ice before the change."""
    stints = build_stints(_line_change(), {90: "G", 91: "G"}, HOME)
    found = stint_at(stints, second)
    assert found is not None
    assert found.home_skaters == owner


def test_an_event_outside_every_stint_has_no_owner() -> None:
    stints = build_stints(_line_change(), {90: "G", 91: "G"}, HOME)
    assert stint_at(stints, 101) is None
    assert stint_at(stints, 0 - 1) is None


def test_a_stint_does_not_cross_a_period_boundary() -> None:
    intervals = [_iv(1, HOME, 0, 1200, period=1), _iv(1, HOME, 1200, 2400, period=2)]
    stints = build_stints(intervals, {}, HOME)
    assert [(s.period, s.start, s.end) for s in stints] == [(1, 0, 1200), (2, 1200, 2400)]
    # The first second of period 2 belongs to the first stint of period 2, and the last second
    # of period 1 to the last stint of period 1.
    assert stint_at(stints, 1200) == stints[0]
    first_of_two = [s for s in stints if s.period == 2][0]
    assert stint_at(stints, 1201) == first_of_two


def test_the_first_second_of_a_period_belongs_to_its_first_stint() -> None:
    intervals = [_iv(1, HOME, 0, 50), _iv(1, HOME, 1200, 1250, period=2)]
    stints = build_stints(intervals, {}, HOME)
    found = stint_at(stints, 1200)
    assert found is not None
    assert found.period == 2


def test_a_team_with_no_goalie_on_the_ice_has_an_empty_net() -> None:
    intervals = [_iv(1, HOME, 0, 60), _iv(91, AWAY, 0, 60)]
    (stint,) = build_stints(intervals, {91: "G"}, HOME)
    assert (stint.home_goalie, stint.away_goalie) == (None, 91)
    assert stint.home_skaters == (1,)


def test_an_intermission_is_not_a_stint() -> None:
    intervals = [_iv(1, HOME, 0, 60), _iv(1, HOME, 1200, 1260, period=2)]
    assert len(build_stints(intervals, {}, HOME)) == 2


_PLAYERS = st.integers(min_value=1, max_value=6)
_SHIFTS = st.lists(
    st.tuples(_PLAYERS, st.sampled_from([HOME, AWAY]), st.integers(0, 1100), st.integers(1, 100)),
    min_size=1,
    max_size=25,
)


@given(_SHIFTS)
def test_the_stints_cover_each_players_merged_time_exactly_once(
    shifts: list[tuple[int, str, int, int]],
) -> None:
    """A player id is unique per team, so the team code is part of the key."""
    intervals = [_iv(p * (1 if team == HOME else 100), team, s, s + n) for p, team, s, n in shifts]
    merged, _ = merge_intervals(intervals)
    stints = build_stints(merged, {}, HOME)
    for interval in merged:
        covered = sum(
            s.end - s.start
            for s in stints
            if interval.player_id in (s.home_skaters + s.away_skaters)
            and s.start >= interval.start
            and s.end <= interval.end
        )
        assert covered == interval.end - interval.start
    union = sorted({t for i in merged for t in range(i.start, i.end)})
    assert sum(s.end - s.start for s in stints) == len(union)
    assert all(a.end <= b.start for a, b in zip(stints, stints[1:], strict=False))
