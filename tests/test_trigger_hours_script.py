"""Tests for scripts/trigger_hours.py, which derives the report trigger hours."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

from hockey_model.report_trigger import TRIGGER_MINUTE_UTC, puck_drop_ahead

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "trigger_hours.py")


def _load() -> ModuleType:
    """Import the script by path, because scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("trigger_hours", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOURS = _load()


def _week(*games: tuple[str, str, int]) -> dict[str, object]:
    days: dict[str, list[dict[str, object]]] = {}
    for day, start, game_type in games:
        days.setdefault(day, []).append({"startTimeUTC": start, "gameType": game_type})
    return {"gameWeek": [{"date": day, "games": rows} for day, rows in days.items()]}


def test_first_starts_takes_the_earliest_game_of_each_game_day() -> None:
    week = _week(
        ("2026-11-04", "2026-11-05T03:00:00Z", 2),
        ("2026-11-04", "2026-11-05T00:00:00Z", 2),
        ("2026-11-05", "2026-11-05T17:00:00Z", 2),
    )
    firsts = HOURS.first_starts([week], {2})
    assert firsts == {
        "2026-11-04": datetime(2026, 11, 5, 0, 0, tzinfo=UTC),
        "2026-11-05": datetime(2026, 11, 5, 17, 0, tzinfo=UTC),
    }


def test_first_starts_skips_other_game_types() -> None:
    week = _week(
        ("2026-09-30", "2026-09-30T23:00:00Z", 1), ("2026-10-07", "2026-10-07T23:00:00Z", 2)
    )
    assert list(HOURS.first_starts([week], {2})) == ["2026-10-07"]


def test_summary_names_each_trigger_hour_once() -> None:
    firsts = {
        "2026-11-04": datetime(2026, 11, 5, 0, 0, tzinfo=UTC),
        "2026-11-05": datetime(2026, 11, 6, 0, 30, tzinfo=UTC),
        "2026-11-06": datetime(2026, 11, 7, 0, 0, tzinfo=UTC),
    }
    summary = HOURS.summarize(firsts, 44)
    assert summary["minute"] == 44
    assert summary["hours"] == [22, 23]
    assert summary["days_per_hour"] == {"22": 2, "23": 1}
    assert summary["first_puck_drops_et"] == {"19:00 EST": 2, "19:30 EST": 1}


_UTC_TIMES = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2099, 1, 1), timezones=st.just(UTC)
)


@given(_UTC_TIMES, st.integers(min_value=0, max_value=59))
def test_the_trigger_is_the_slot_before_the_puck_drop_moved_earlier(
    first: datetime, minute: int
) -> None:
    hour = HOURS.trigger_hour_for(first, minute)
    slot = first.replace(minute=0, second=0, microsecond=0)
    if slot >= first:
        slot -= timedelta(hours=1)
    trigger = slot - timedelta(minutes=60 - minute)
    assert trigger.hour == hour
    assert timedelta(minutes=60 - minute) < first - trigger <= timedelta(minutes=120 - minute)


@pytest.mark.parametrize(
    ("first", "hour"),
    [
        (datetime(2026, 11, 5, 0, 0, tzinfo=UTC), 22),
        (datetime(2026, 11, 5, 0, 30, tzinfo=UTC), 23),
        (datetime(2026, 11, 4, 13, 0, tzinfo=UTC), 11),
    ],
)
def test_derived_hours_match_the_runtime_rule(first: datetime, hour: int) -> None:
    assert HOURS.trigger_hour_for(first, TRIGGER_MINUTE_UTC) == hour
    day = first - timedelta(days=1) if hour > first.hour else first
    trigger = day.replace(hour=hour, minute=TRIGGER_MINUTE_UTC)
    assert puck_drop_ahead(trigger, first)


def test_minute_out_of_range_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        HOURS.main(["--from", "2026-11-04", "--to", "2026-11-05", "--minute", "60"])
    assert "--minute must be 0 to 59" in capsys.readouterr().err


def test_to_before_from_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        HOURS.main(["--from", "2026-11-05", "--to", "2026-11-04"])
    assert "is before --from" in capsys.readouterr().err
