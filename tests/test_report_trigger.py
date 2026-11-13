"""Tests for the trigger that decides when the daily report runs."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.game_day import game_day_window
from hockey_model.report_trigger import (
    PREP_WINDOW,
    TRIGGER_HOURS_UTC,
    TRIGGER_MINUTE_UTC,
    first_puck_drop,
    prep_too_early,
    puck_drop_ahead,
)
from hockey_model.types import HockeyModelError

_UTC_TIMES = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2099, 12, 31, 23, 59),
    timezones=st.just(UTC),
)
_DAYS = st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 30))


def at(text: str) -> datetime:
    """Parse ``2026-11-04T12:30`` as UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


DAY = date(2026, 11, 4)
CLOSES = game_day_window(DAY)[1]


@pytest.mark.parametrize(
    ("first", "runs_at", "skips_at"),
    [
        # 7 pm EST slate: 00:00 UTC. A run before the puck drop is due, a run after it is not.
        ("2026-11-05T00:00", "2026-11-04T23:44", "2026-11-05T00:44"),
        # 2 pm EST matinee: 19:00 UTC. A run before the puck drop is due, a run after it is not.
        ("2026-11-04T19:00", "2026-11-04T16:44", "2026-11-04T19:44"),
        # 8 am EST international game: 13:00 UTC. A run before the puck drop is due.
        ("2026-11-04T13:00", "2026-11-04T11:44", "2026-11-04T13:44"),
    ],
)
def test_a_run_before_the_puck_drop_is_due_and_a_run_after_it_is_not(
    first: str, runs_at: str, skips_at: str
) -> None:
    assert puck_drop_ahead(at(runs_at), at(first)) is True
    assert puck_drop_ahead(at(skips_at), at(first)) is False


def test_a_run_that_starts_after_the_puck_drop_is_not_due() -> None:
    assert puck_drop_ahead(at("2026-11-05T00:31"), at("2026-11-05T00:30")) is False


def test_no_games_runs_until_the_end_of_the_game_day() -> None:
    first = first_puck_drop(CLOSES, [])
    assert first == at("2026-11-05T05:00")
    assert puck_drop_ahead(at("2026-11-04T23:44"), first) is True
    assert puck_drop_ahead(at("2026-11-05T00:44"), first) is True
    assert puck_drop_ahead(at("2026-11-05T05:00"), first) is False


def test_first_puck_drop_is_the_earliest_start() -> None:
    starts = [at("2026-11-05T03:00"), at("2026-11-05T00:00"), at("2026-11-05T00:30")]
    assert first_puck_drop(CLOSES, starts) == at("2026-11-05T00:00")


def test_trigger_hours_are_sorted_unique_hours_and_the_minute_is_valid() -> None:
    assert list(TRIGGER_HOURS_UTC) == sorted(set(TRIGGER_HOURS_UTC))
    assert all(0 <= hour <= 23 for hour in TRIGGER_HOURS_UTC)
    assert 0 <= TRIGGER_MINUTE_UTC <= 59


def test_naive_time_is_an_error() -> None:
    with pytest.raises(HockeyModelError, match="must be a timezone-aware UTC datetime"):
        puck_drop_ahead(datetime(2026, 11, 4, 12, 0), at("2026-11-05T00:00"))


def test_non_utc_offset_is_an_error() -> None:
    eastern = datetime(2026, 11, 4, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    with pytest.raises(HockeyModelError, match="offset"):
        puck_drop_ahead(eastern, at("2026-11-05T00:00"))


@given(_DAYS, st.lists(_UTC_TIMES, max_size=8))
def test_first_puck_drop_is_the_minimum_or_the_day_close(day: date, starts: list[datetime]) -> None:
    closes = game_day_window(day)[1]
    result = first_puck_drop(closes, starts)
    if starts:
        assert result in starts
        assert all(result <= start for start in starts)
    else:
        assert result == closes


@given(_UTC_TIMES, _UTC_TIMES)
def test_puck_drop_ahead_is_true_exactly_before_the_start(now: datetime, start: datetime) -> None:
    assert puck_drop_ahead(now, start) is (now < start)
    if puck_drop_ahead(now, start):
        assert puck_drop_ahead(now - timedelta(minutes=1), start)


def test_a_margin_moves_the_cutoff_earlier_by_its_length() -> None:
    first = at("2026-11-05T00:00")
    # A run at 23:49 with a 10-minute margin finishes at 23:59, so it is still due.
    assert puck_drop_ahead(at("2026-11-04T23:49"), first, timedelta(minutes=10)) is True
    # A run at 23:51 would finish at 00:01, after the puck drop, so it is not due.
    assert puck_drop_ahead(at("2026-11-04T23:51"), first, timedelta(minutes=10)) is False


def test_a_negative_margin_is_an_error() -> None:
    with pytest.raises(HockeyModelError, match="must not be negative"):
        puck_drop_ahead(at("2026-11-04T23:00"), at("2026-11-05T00:00"), timedelta(seconds=-1))


# A run this far before the first puck drop is too early to call the prep missing an error:
# the morning prep job has not had its window yet.
@pytest.mark.parametrize(
    ("now", "first", "expected"),
    [
        # Hours before the puck drop, and the prep has not run: too early, skip.
        ("2026-11-13T07:24", "2026-11-13T23:00", True),
        # Inside the window: a missing prep is a real failure, so it is not too early.
        ("2026-11-13T16:00", "2026-11-13T19:00", False),
        # Exactly the window before the puck drop is still inside it.
        ("2026-11-13T10:00", "2026-11-13T13:00", False),
        # One second more than the window is outside it.
        ("2026-11-13T09:59:59", "2026-11-13T13:00", True),
        # After the puck drop, the prep failure is real.
        ("2026-11-13T20:00", "2026-11-13T19:00", False),
    ],
)
def test_prep_is_too_early_only_more_than_the_prep_window_before_the_puck_drop(
    now: str, first: str, expected: bool
) -> None:
    assert prep_too_early(at(now), at(first)) is expected


def test_prep_window_matches_the_prepare_job_timeout() -> None:
    # daily-prepare.yml sets timeout-minutes: 180. A prep that starts at its cron can run that long.
    assert PREP_WINDOW == timedelta(minutes=180)


def test_prep_too_early_rejects_a_naive_time() -> None:
    with pytest.raises(HockeyModelError):
        prep_too_early(datetime(2026, 11, 13, 7, 24), at("2026-11-13T23:00"))
