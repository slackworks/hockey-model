"""Tests for the trigger that decides when the daily report runs."""

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

from hockey_model.game_day import game_day_of, game_day_window
from hockey_model.report_trigger import (
    PREP_CRON_HOUR_UTC,
    PREP_WINDOW,
    TRIGGER_HOURS_UTC,
    TRIGGER_MINUTE_UTC,
    first_puck_drop,
    prep_blocks_report,
    prep_too_early,
    prep_window_opened,
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
# the morning prep job has not had its window yet. The window opens at the prep cron, so it
# closes three hours after 08:00 UTC whatever the puck drop is (#163).
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
    assert prep_too_early(at(now), at(first), date(2026, 11, 13)) is expected


@pytest.mark.parametrize(
    ("now", "first", "expected"),
    [
        # 2026-11-19: the prep ran from 17:06 to 19:17 with no marker, and the report skipped
        # every slot before that. After 12:00 UTC the window is closed, and the missing marker
        # is a failure that a person has to see (#163).
        ("2026-11-19T12:01", "2026-11-20T05:00", False),
        # The last minute of the window still skips, because a late prep can still finish.
        ("2026-11-19T12:00", "2026-11-20T05:00", True),
        # On the same day a run before the cron skips too.
        ("2026-11-19T07:44", "2026-11-20T05:00", True),
    ],
)
def test_a_late_puck_drop_does_not_keep_the_prep_window_open(
    now: str, first: str, expected: bool
) -> None:
    """The window closes three hours after the prep cron, whatever the puck drop is."""
    assert prep_too_early(at(now), at(first), date(2026, 11, 19)) is expected


def test_the_prep_window_opens_at_the_prepare_cron() -> None:
    assert prep_window_opened(date(2026, 11, 13)) == at("2026-11-13T08:00")
    assert PREP_WINDOW == timedelta(minutes=180)


def test_prep_window_matches_the_prepare_job_timeout() -> None:
    # The prep job timeout in daily-prepare.yml is the window the prep can use.
    prepare = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily-prepare.yml"
    workflow = require_path(prepare).read_text(encoding="utf-8")
    assert f"timeout-minutes: {PREP_WINDOW // timedelta(minutes=1)}" in workflow
    assert f'cron: "0 {PREP_CRON_HOUR_UTC} * * *"' in workflow


def test_prep_too_early_rejects_a_naive_time() -> None:
    with pytest.raises(HockeyModelError):
        prep_too_early(datetime(2026, 11, 13, 7, 24), at("2026-11-13T23:00"), date(2026, 11, 13))


def test_a_negative_queue_allowance_is_an_error() -> None:
    with pytest.raises(HockeyModelError, match="must not be negative"):
        prep_too_early(
            at("2026-11-13T07:24"), at("2026-11-13T23:00"), date(2026, 11, 13), timedelta(-1)
        )


@given(
    now=_UTC_TIMES,
    extra=st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=3)),
)
def test_prep_too_early_is_true_only_before_the_window_closes(
    now: datetime, extra: timedelta
) -> None:
    # The window closes ``PREP_WINDOW + extra`` after the cron, whatever the puck drop is.
    day = game_day_of(now)
    closes = prep_window_opened(day) + PREP_WINDOW + extra
    first = max(now, closes) + PREP_WINDOW + timedelta(hours=1)
    assert prep_too_early(now, first, day, extra) is (now <= closes)


@given(
    prep=st.datetimes(timezones=st.just(UTC)),
    gap=st.timedeltas(min_value=timedelta(0), max_value=timedelta(hours=12)),
)
def test_a_prepare_blocks_a_report_only_when_its_window_reaches_the_puck_drop(
    prep: datetime, gap: timedelta
) -> None:
    first = prep + gap
    assert prep_blocks_report(prep, first) is (prep + PREP_WINDOW > first)


def test_a_prepare_that_started_on_time_does_not_block_the_report() -> None:
    # The 08:00 cron plus the 180 minute timeout ends at 11:00 UTC, before the 11:44 trigger.
    prep = at("2026-11-18T08:00")
    assert prep_blocks_report(prep, at("2026-11-18T11:44")) is False


def test_a_prepare_that_started_late_blocks_a_report_after_the_puck_drop() -> None:
    # 2026-11-18: the 08:00 prep started at 18:24 UTC and ran 128 minutes, so the 15:44
    # report that queued behind it started at 20:32, after the first puck drop (#163).
    prep = at("2026-11-18T18:24")
    assert prep_blocks_report(prep, at("2026-11-18T19:00")) is True


@given(
    now=_UTC_TIMES,
    margin=st.timedeltas(min_value=timedelta(0), max_value=timedelta(hours=6)),
    gap=st.timedeltas(min_value=timedelta(0), max_value=timedelta(hours=12)),
)
def test_puck_drop_ahead_counts_the_margin_before_the_puck_drop(
    now: datetime, margin: timedelta, gap: timedelta
) -> None:
    assert puck_drop_ahead(now, now + gap, margin) is (now + margin < now + gap)
