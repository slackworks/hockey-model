"""Tests for the trigger that decides when the daily report runs."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.game_day import game_day_window
from hockey_model.report_trigger import (
    TRIGGER_MINUTE_UTC,
    first_puck_drop,
    prep_blocks_report,
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


def test_the_trigger_minute_is_valid() -> None:
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


@given(
    prep=st.datetimes(timezones=st.just(UTC)),
    gap=st.timedeltas(min_value=timedelta(0), max_value=timedelta(hours=12)),
    margin=st.timedeltas(min_value=timedelta(0), max_value=timedelta(hours=6)),
    window=st.timedeltas(min_value=timedelta(minutes=1), max_value=timedelta(hours=6)),
)
def test_a_prepare_blocks_a_report_only_when_its_window_reaches_the_puck_drop(
    prep: datetime, gap: timedelta, margin: timedelta, window: timedelta
) -> None:
    """The window is the timeout of the job that holds the group, which the lookup reads."""
    first = prep + gap
    assert prep_blocks_report(prep + window, first, margin) is (prep + window + margin >= first)


def test_a_short_skip_job_does_not_block_a_report_before_the_puck_drop() -> None:
    """A skip job of a prepared day ends in minutes, so it holds nothing past the puck drop (#311).

    Without the split, the same run would read as a 180-minute rebuild and skip the report in
    the hours before the puck drop, which is when the starters are confirmed.
    """
    started = at("2026-11-18T18:24")
    first = at("2026-11-18T19:00")
    assert prep_blocks_report(started + timedelta(minutes=180), first) is True
    assert prep_blocks_report(started + timedelta(minutes=10), first) is False


def test_the_report_margin_extends_the_hold_that_a_prepare_has() -> None:
    # The prepare ends at 21:24, and the report then needs its own 10 minutes, so a puck drop
    # at 21:30 is still lost. Without the margin the same puck drop looks safe.
    ends = at("2026-11-18T21:24")
    assert prep_blocks_report(ends, at("2026-11-18T21:30")) is False
    assert prep_blocks_report(ends, at("2026-11-18T21:30"), timedelta(minutes=10)) is True
    assert prep_blocks_report(ends, at("2026-11-18T21:34:01"), timedelta(minutes=10)) is False


def test_a_prepare_blocks_a_report_that_would_finish_at_the_puck_drop() -> None:
    """A report that ends at the puck drop cannot replace a published one, so it is held."""
    ends = at("2026-11-18T11:00")
    assert prep_blocks_report(ends, at("2026-11-18T11:00")) is True
    assert prep_blocks_report(ends, at("2026-11-18T11:00:01")) is False


def test_prep_blocks_report_rejects_a_negative_margin() -> None:
    with pytest.raises(HockeyModelError, match="must not be negative"):
        prep_blocks_report(at("2026-11-18T08:00"), at("2026-11-18T19:00"), timedelta(-1))


def test_prep_blocks_report_rejects_a_local_time() -> None:
    """A naive instant would compare against UTC midnight, and no reading would catch that."""
    with pytest.raises(HockeyModelError, match="UTC"):
        prep_blocks_report(datetime(2026, 11, 18, 8), at("2026-11-18T19:00"))


def test_a_prepare_that_started_on_time_does_not_block_the_report() -> None:
    # The 08:00 job plus its 180-minute timeout ends at 11:00 UTC, before the 11:44 trigger.
    ends = at("2026-11-18T08:00") + timedelta(minutes=180)
    assert prep_blocks_report(ends, at("2026-11-18T11:44")) is False


def test_a_prepare_that_started_late_blocks_a_report_after_the_puck_drop() -> None:
    # 2026-11-18: the 08:00 prep started at 18:24 UTC and ran 128 minutes, so the 15:44
    # report that queued behind it started at 20:32, after the first puck drop (#163).
    ends = at("2026-11-18T18:24") + timedelta(minutes=180)
    assert prep_blocks_report(ends, at("2026-11-18T19:00")) is True


@given(
    now=_UTC_TIMES,
    margin=st.timedeltas(min_value=timedelta(0), max_value=timedelta(hours=6)),
    gap=st.timedeltas(min_value=timedelta(0), max_value=timedelta(hours=12)),
)
def test_puck_drop_ahead_counts_the_margin_before_the_puck_drop(
    now: datetime, margin: timedelta, gap: timedelta
) -> None:
    assert puck_drop_ahead(now, now + gap, margin) is (now + margin < now + gap)
