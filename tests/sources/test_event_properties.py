"""Property tests for the event parsers: any input gives rows or a named error, and nothing else."""

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from json_mutation import mutations

from hockey_model.sources.espn_summary import parse_summary
from hockey_model.sources.nhl_events import (
    MAX_PERIOD,
    PERIOD_SECONDS,
    _direction,
    clock_seconds,
    game_second,
    parse_pbp,
)
from hockey_model.sources.nhl_shifts import parse_shifts
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError

FIXTURES = Path(__file__).parent.parent / "fixtures"
PBP = json.loads((FIXTURES / "nhl" / "pbp_2025020709.json").read_text())
SHIFTS = json.loads((FIXTURES / "nhl" / "shifts_2025020709.json").read_text())
ESPN = json.loads((FIXTURES / "espn" / "summary_401803060.json").read_text())
GAME = 2025020709
EVENT = "401803060"
NAMED = (SourceParseError, SourceShapeError)
SLOW = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@given(total=st.integers(0, PERIOD_SECONDS))
def test_a_clock_reads_back_to_its_seconds(total: int) -> None:
    assert clock_seconds(f"{total // 60:02d}:{total % 60:02d}", "x") == total


@given(st.text(max_size=8))
def test_a_clock_text_gives_a_time_in_the_period_or_a_named_error(text: str) -> None:
    try:
        seconds = clock_seconds(text, "x")
    except SourceParseError:
        return
    assert 0 <= seconds <= PERIOD_SECONDS


@given(period=st.integers(1, MAX_PERIOD - 1), second=st.integers(0, PERIOD_SECONDS - 1))
def test_the_second_of_the_game_grows_with_the_clock_and_the_period(
    period: int, second: int
) -> None:
    assert game_second(period, second) < game_second(period, second + 1)
    assert game_second(period, second) < game_second(period + 1, second)
    assert game_second(period + 1, 0) == game_second(period, PERIOD_SECONDS)


@given(side=st.sampled_from(["left", "right"]))
def test_the_two_teams_attack_opposite_ways_and_a_side_swap_flips_both(side: str) -> None:
    flipped = "right" if side == "left" else "left"
    home = _direction({"homeTeamDefendingSide": side}, "HOM", "HOM", "x")
    away = _direction({"homeTeamDefendingSide": side}, "AWY", "HOM", "x")
    assert home is not None
    assert away == -home
    assert _direction({"homeTeamDefendingSide": flipped}, "HOM", "HOM", "x") == -home


@SLOW
@given(payload=mutations(PBP))
def test_a_changed_play_by_play_gives_rows_or_a_named_error(payload: object) -> None:
    try:
        rows = parse_pbp(payload, GAME)
    except NAMED:
        return
    assert rows.game_id == GAME
    assert rows.events
    assert all(e.game_second is None or e.game_second >= 0 for e in rows.events)


@SLOW
@given(payload=mutations(SHIFTS))
def test_a_changed_shift_chart_gives_rows_or_a_named_error(payload: object) -> None:
    try:
        rows = parse_shifts(payload, GAME)
    except NAMED:
        return
    assert rows.shifts
    assert all(0 <= s.start_second and 0 <= s.end_second for s in rows.shifts)


@SLOW
@given(payload=mutations(ESPN))
def test_a_changed_espn_summary_gives_rows_or_a_named_error(payload: object) -> None:
    try:
        rows = parse_summary(payload, EVENT)
    except NAMED:
        return
    assert rows.skaters
    assert all(s.toi >= 0 for s in rows.skaters)
