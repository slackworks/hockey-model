"""Tests for the stint builder: merge, split, and the rule that owns a boundary event (#35)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.stints import Interval, build_stints, merge_intervals, stint_at, stints_around
from hockey_model.types import HockeyModelError

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


def test_a_generator_of_intervals_is_read_once_and_in_full() -> None:
    merged, dropped = merge_intervals(i for i in [_iv(1, HOME, 0, 10), _iv(2, HOME, 5, 5)])
    assert [i.player_id for i in merged] == [1]
    assert [i.player_id for i in dropped] == [2]


def test_one_player_id_on_two_teams_stays_apart() -> None:
    merged, _ = merge_intervals([_iv(1, HOME, 0, 30), _iv(1, AWAY, 10, 40)])
    assert len(merged) == 2


def test_the_same_player_in_two_periods_stays_apart() -> None:
    merged, _ = merge_intervals([_iv(1, HOME, 0, 30, 1), _iv(1, HOME, 1200, 1230, 2)])
    assert len(merged) == 2


def _line_change() -> list[Interval]:
    """Home skaters 1 and 2 play 0..100. Skater 3 replaces skater 2 at 60. The goalies stay."""
    return [
        _iv(1, HOME, 0, 100),
        _iv(2, HOME, 0, 60),
        _iv(3, HOME, 60, 100),
        _iv(20, AWAY, 0, 100),
        _iv(90, HOME, 0, 100),
        _iv(91, AWAY, 0, 100),
    ]


def test_a_stint_splits_at_each_change_of_players() -> None:
    result = build_stints(_line_change(), {90: "G", 91: "G"}, HOME, AWAY)
    assert [(s.start, s.end, s.home_skaters) for s in result.stints] == [
        (0, 60, (1, 2)),
        (60, 100, (1, 3)),
    ]
    assert all(s.home_goalies == (90,) and s.away_goalies == (91,) for s in result.stints)
    assert all(s.away_skaters == (20,) for s in result.stints)


def test_the_builder_merges_the_shifts_and_reports_the_dropped_ones() -> None:
    shifts = [_iv(1, HOME, 0, 50), _iv(1, HOME, 50, 100), _iv(2, HOME, 70, 70)]
    result = build_stints(shifts, {}, HOME, AWAY)
    assert [(s.start, s.end, s.home_skaters) for s in result.stints] == [(0, 100, (1,))]
    assert (result.zero_length, result.negative_length) == (1, 0)


def test_a_shift_that_ends_before_it_starts_is_counted_apart_from_a_zero_length_one() -> None:
    shifts = [_iv(1, HOME, 0, 50), _iv(2, HOME, 60, 60), _iv(3, HOME, 70, 40), _iv(4, HOME, 80, 20)]
    result = build_stints(shifts, {}, HOME, AWAY)
    assert (result.zero_length, result.negative_length) == (1, 2)


def test_neighbors_with_the_same_players_join_into_one_stint() -> None:
    shifts = [_iv(1, HOME, 0, 40), _iv(1, HOME, 40, 80), _iv(2, AWAY, 0, 80)]
    result = build_stints(shifts, {}, HOME, AWAY)
    assert [(s.start, s.end) for s in result.stints] == [(0, 80)]


def test_a_gap_with_nobody_on_the_ice_is_not_a_stint_and_does_not_join() -> None:
    shifts = [_iv(1, HOME, 0, 40), _iv(1, HOME, 60, 100)]
    result = build_stints(shifts, {}, HOME, AWAY)
    assert [(s.start, s.end) for s in result.stints] == [(0, 40), (60, 100)]


def test_a_stint_never_crosses_a_period() -> None:
    shifts = [_iv(1, HOME, 0, 1200, 1), _iv(1, HOME, 1200, 2400, 2)]
    result = build_stints(shifts, {}, HOME, AWAY)
    assert [(s.period, s.start, s.end) for s in result.stints] == [(1, 0, 1200), (2, 1200, 2400)]


def test_two_goalies_on_the_ice_at_once_are_both_kept() -> None:
    shifts = [_iv(90, HOME, 0, 100), _iv(92, HOME, 90, 110), _iv(1, HOME, 0, 110)]
    result = build_stints(shifts, {90: "G", 92: "G"}, HOME, AWAY)
    overlap = next(s for s in result.stints if s.start == 90)
    assert overlap.home_goalies == (90, 92)
    assert overlap.home_skaters == (1,)


def test_a_player_that_the_roster_does_not_name_counts_as_a_skater() -> None:
    result = build_stints([_iv(7, HOME, 0, 10)], {}, HOME, AWAY)
    assert result.stints[0].home_skaters == (7,)


def test_a_shift_for_a_third_team_is_a_named_error() -> None:
    with pytest.raises(HockeyModelError, match=r"player 5 .* neither the home team EDM"):
        build_stints([_iv(5, "CGY", 0, 10)], {}, HOME, AWAY)


@pytest.mark.parametrize(
    ("period", "second", "owner"),
    [
        (1, 1, 0),
        (1, 60, 0),
        (1, 61, 1),
        (1, 100, 1),
        (1, 0, 0),
        (1, 101, None),
        (2, 1200, 2),
        (2, 1260, 2),
        (3, 50, None),
    ],
)
def test_an_event_belongs_to_the_stint_that_ends_at_or_after_it(
    period: int, second: int, owner: int | None
) -> None:
    shifts = [
        _iv(1, HOME, 0, 60),
        _iv(2, HOME, 60, 100),
        _iv(3, HOME, 1200, 1300, 2),
    ]
    stints = build_stints(shifts, {}, HOME, AWAY).stints
    found = stint_at(stints, period, second)
    assert found is None if owner is None else found == stints[owner]


def test_a_goal_at_a_line_change_belongs_to_the_players_before_the_change() -> None:
    stints = build_stints(_line_change(), {90: "G", 91: "G"}, HOME, AWAY).stints
    before = stint_at(stints, 1, 60)
    assert before is not None
    assert before.home_skaters == (1, 2)


def test_the_first_second_of_a_period_belongs_to_the_first_stint_of_that_period() -> None:
    shifts = [_iv(1, HOME, 0, 1200, 1), _iv(2, HOME, 1200, 2400, 2)]
    stints = build_stints(shifts, {}, HOME, AWAY).stints
    first = stint_at(stints, 2, 1200)
    assert first is not None
    assert first.home_skaters == (2,)
    last_of_one = stint_at(stints, 1, 1200)
    assert last_of_one is not None
    assert last_of_one.home_skaters == (1,)


players = st.integers(min_value=1, max_value=6)
spans = st.tuples(st.integers(0, 60), st.integers(0, 60)).map(lambda p: (min(p), max(p)))
shifts_strategy = st.lists(
    st.tuples(players, st.sampled_from([HOME, AWAY]), st.integers(1, 2), spans),
    max_size=14,
)


@given(shifts_strategy)
def test_the_stints_cover_the_merged_time_of_each_player_exactly_once(
    raw: list[tuple[int, str, int, tuple[int, int]]],
) -> None:
    intervals = [_iv(p, t, 1200 * (n - 1) + a, 1200 * (n - 1) + b, n) for p, t, n, (a, b) in raw]
    merged, _ = merge_intervals(intervals)
    stints = build_stints(intervals, {}, HOME, AWAY).stints
    for interval in merged:
        covered = sum(
            s.end - s.start
            for s in stints
            if s.period == interval.period
            and interval.player_id in (s.home_skaters if interval.team == HOME else s.away_skaters)
            and interval.start <= s.start
            and s.end <= interval.end
        )
        assert covered == interval.end - interval.start
    for left, right in zip(stints, stints[1:], strict=False):
        assert (left.period, left.end) <= (right.period, right.start)
        same = (left.home_skaters, left.away_skaters) == (right.home_skaters, right.away_skaters)
        assert not (same and left.period == right.period and left.end == right.start)


def test_an_event_at_a_boundary_has_the_stint_before_and_the_stint_after() -> None:
    stints = build_stints(_line_change(), {90: "G", 91: "G"}, HOME, AWAY).stints
    around = stints_around(stints, 1, 60)
    assert [(s.start, s.end) for s in around] == [(0, 60), (60, 100)]


def test_an_event_inside_a_stint_has_that_stint_only() -> None:
    stints = build_stints(_line_change(), {90: "G", 91: "G"}, HOME, AWAY).stints
    assert [(s.start, s.end) for s in stints_around(stints, 1, 30)] == [(0, 60)]


def test_an_event_at_the_end_of_a_period_has_no_stint_after() -> None:
    shifts = [_iv(1, HOME, 0, 1200, 1), _iv(2, HOME, 1200, 2400, 2)]
    stints = build_stints(shifts, {}, HOME, AWAY).stints
    assert [(s.period, s.end) for s in stints_around(stints, 1, 1200)] == [(1, 1200)]


def test_an_event_in_no_stint_has_no_stints_around_it() -> None:
    stints = build_stints([_iv(1, HOME, 0, 60)], {}, HOME, AWAY).stints
    assert stints_around(stints, 1, 500) == ()
    assert stints_around(stints, 2, 30) == ()


raw_intervals = st.lists(
    st.tuples(
        players,
        st.sampled_from([HOME, AWAY]),
        st.integers(1, 2),
        st.integers(0, 40),
        st.integers(0, 40),
    ),
    max_size=14,
).map(lambda rows: [_iv(p, t, a, b, n) for p, t, n, a, b in rows])


def _seconds(intervals: list[Interval]) -> set[tuple[int, int, str, int]]:
    return {
        (i.period, i.player_id, i.team, second)
        for i in intervals
        if i.end > i.start
        for second in range(i.start, i.end)
    }


@given(raw_intervals)
def test_the_merge_keeps_the_covered_time_and_drops_only_the_empty_intervals(
    intervals: list[Interval],
) -> None:
    merged, dropped = merge_intervals(intervals)
    assert _seconds(merged) == _seconds(intervals)
    assert dropped == [i for i in intervals if i.end <= i.start]


@given(raw_intervals)
def test_the_merge_is_idempotent_and_leaves_gaps_between_the_intervals_of_a_player(
    intervals: list[Interval],
) -> None:
    merged, _ = merge_intervals(intervals)
    again, dropped = merge_intervals(merged)
    assert again == merged
    assert dropped == []
    for left, right in zip(merged, merged[1:], strict=False):
        if (left.period, left.player_id, left.team) == (right.period, right.player_id, right.team):
            assert right.start > left.end


@given(raw_intervals)
def test_a_second_has_at_most_one_owner_and_stint_at_finds_it(intervals: list[Interval]) -> None:
    stints = build_stints(intervals, {}, HOME, AWAY).stints
    for period in (1, 2):
        mine = [s for s in stints if s.period == period]
        for second in range(-1, 45):
            owners = [s for s in mine if s.start < second <= s.end]
            assert len(owners) <= 1
            first = mine[0] if mine and second == mine[0].start else None
            expected = owners[0] if owners else first
            assert stint_at(stints, period, second) == expected


@given(raw_intervals, st.integers(1, 2), st.integers(-1, 45))
def test_the_stints_around_an_event_are_its_owner_and_the_stint_that_starts_there(
    intervals: list[Interval], period: int, second: int
) -> None:
    stints = build_stints(intervals, {}, HOME, AWAY).stints
    owner = stint_at(stints, period, second)
    around = stints_around(stints, period, second)
    if owner is None:
        assert around == ()
        return
    assert around[0] == owner
    assert len(around) in (1, 2)
    if len(around) == 2:
        assert (around[1].period, around[1].start) == (period, second)
