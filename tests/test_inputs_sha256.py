"""Tests that the report fingerprint follows what a reader sees and ignores the run time."""

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from seeding import utc

from hockey_model.daily_report import (
    DailyLine,
    DailyReport,
    ModelIdentity,
    inputs_sha256,
    report_json,
)
from hockey_model.game import (
    DegradedInput,
    GamePrediction,
    MissingInput,
    MissingKind,
    TrainingInfo,
    TrainingMembership,
)
from hockey_model.sources.nhl import GameRow
from hockey_model.store.source_reads import record_read
from hockey_model.store.staleness import stale_sources
from hockey_model.types import Projection

DAY = date(2026, 1, 20)
NOW = utc("2026-01-20T12:00")
POINT = Projection(3.0, 2.0, 4.0, 0.8)
GOALIE = MissingInput(MissingKind.GOALIE_UNCONFIRMED, "TOR", "starter is expected, not confirmed")
STALE_NOTE = "espn: the newest good read is {} hours old"
STALE_12 = MissingInput(MissingKind.STALE_SOURCE, None, STALE_NOTE.format(12))
STALE_13 = MissingInput(MissingKind.STALE_SOURCE, None, STALE_NOTE.format(13))


def _row(**fields: object) -> GameRow:
    row = GameRow(10, 20252026, 2, utc("2026-01-20T23:00"), "TOR", "BOS", "FUT", None, None, None)
    return replace(row, **fields)


def _prediction(**fields: object) -> GamePrediction:
    base = GamePrediction(0.55, 0.45, POINT, POINT, (), ("src",), False, (), None)
    return replace(base, **fields)


def _report(line: DailyLine | None = None, **fields: object) -> DailyReport:
    line = line or DailyLine(_row(), _prediction(), "")
    base = DailyReport(
        DAY,
        NOW,
        0.8,
        (line,),
        ModelIdentity(("xg_form",), "f" * 64, "c0ffee"),
        ("espn: gap",),
    )
    return replace(base, **fields)


def _with(**fields: object) -> Callable[[], DailyReport]:
    return lambda: _report(DailyLine(_row(), _prediction(**fields), ""))


CHANGES: dict[str, Callable[[], DailyReport]] = {
    "home_win": _with(home_win=0.56, away_win=0.44),
    "missing": _with(missing=(GOALIE,)),
    "degraded": _with(degraded_inputs=(DegradedInput("rest", MissingKind.REST_UNKNOWN, ("TOR",)),)),
    "made_after_start": _with(made_after_start=True),
    "training": _with(training=TrainingInfo(3, 2025, TrainingMembership.IN_SAMPLE)),
    "game_id": lambda: _report(DailyLine(_row(game_id=11), _prediction(), "")),
    "start": lambda: _report(DailyLine(_row(start=utc("2026-01-20T23:30")), _prediction(), "")),
    "state": lambda: _report(DailyLine(_row(state="LIVE"), _prediction(), "")),
    "skipped": lambda: _report(DailyLine(_row(), None, "game is postponed")),
    "fit": lambda: _report(model=ModelIdentity(("xg_form",), "e" * 64, "c0ffee")),
    "code_sha": lambda: _report(model=ModelIdentity(("xg_form",), "f" * 64, "decaf")),
    "features": lambda: _report(model=ModelIdentity(("rest",), "f" * 64, "c0ffee")),
    "training_gaps": lambda: _report(footer_gaps=()),
    "level": lambda: _report(level=0.9),
    "day": lambda: _report(day=date(2026, 1, 21)),
    "no_games": lambda: replace(_report(), lines=()),
}


def test_the_fingerprint_is_a_sha256_hex_string_and_is_stable() -> None:
    first = inputs_sha256(_report())
    assert len(first) == 64
    assert int(first, 16) >= 0
    assert first == inputs_sha256(_report())


@pytest.mark.parametrize("name", sorted(CHANGES))
def test_each_covered_field_changes_the_fingerprint(name: str) -> None:
    assert inputs_sha256(CHANGES[name]()) != inputs_sha256(_report()), name


def test_the_fingerprint_is_in_the_json_copy() -> None:
    report = _report()
    assert report_json(report)["inputs_sha256"] == inputs_sha256(report)


def test_the_credits_change_the_fingerprint() -> None:
    other = _report(DailyLine(_row(), _prediction(credits=("other",)), ""))
    assert inputs_sha256(other) != inputs_sha256(_report())


def test_a_stale_source_age_does_not_change_the_fingerprint() -> None:
    first = _report(DailyLine(_row(), _prediction(missing=(STALE_12,)), ""))
    later = _report(DailyLine(_row(), _prediction(missing=(STALE_13,)), ""))
    assert inputs_sha256(first) == inputs_sha256(later)


def test_a_new_stale_source_changes_the_fingerprint() -> None:
    first = _report(DailyLine(_row(), _prediction(missing=(STALE_12,)), ""))
    other = MissingInput(MissingKind.STALE_SOURCE, None, "daily_faceoff: no read has worked")
    both = _report(DailyLine(_row(), _prediction(missing=(STALE_12, other)), ""))
    assert inputs_sha256(first) != inputs_sha256(both)


@given(st.integers(min_value=-10_000_000, max_value=10_000_000))
def test_the_fingerprint_ignores_the_time_of_the_run(seconds: int) -> None:
    later = replace(_report(), generated_at=NOW + timedelta(seconds=seconds))
    assert inputs_sha256(later) == inputs_sha256(_report())


@given(st.integers(min_value=0, max_value=10**6), st.integers(min_value=0, max_value=10**6))
def test_any_stale_age_gives_the_same_fingerprint(first: int, second: int) -> None:
    def build(hours: int) -> DailyReport:
        note = MissingInput(MissingKind.STALE_SOURCE, None, STALE_NOTE.format(hours))
        return _report(DailyLine(_row(), _prediction(missing=(note,)), ""))

    assert inputs_sha256(build(first)) == inputs_sha256(build(second))


@given(st.sampled_from(sorted(set(MissingKind) - {MissingKind.STALE_SOURCE})), st.integers(1, 99))
def test_the_age_scrub_leaves_other_notes_alone(kind: MissingKind, hours: int) -> None:
    """A non-stale note that names an age is an input, so a changed age changes the hash."""

    def build(count: int) -> DailyReport:
        note = MissingInput(kind, "TOR", f"{count} hours old")
        return _report(DailyLine(_row(), _prediction(missing=(note,)), ""))

    assert inputs_sha256(build(hours)) != inputs_sha256(build(hours + 1))


@given(
    st.floats(min_value=0.01, max_value=0.99, allow_nan=False),
    st.floats(min_value=0.01, max_value=0.99, allow_nan=False),
)
def test_a_changed_win_chance_changes_the_fingerprint(home: float, other: float) -> None:
    def build(value: float) -> DailyReport:
        return _report(DailyLine(_row(), _prediction(home_win=value, away_win=1 - value), ""))

    assert (inputs_sha256(build(home)) == inputs_sha256(build(other))) is (home == other)


@pytest.mark.parametrize("hours", [25, 26, 49, 100, 1000])
def test_the_stale_notes_of_the_store_are_stable_as_time_passes(
    store: sqlite3.Connection, hours: int
) -> None:
    """The scrub must match the wording that the staleness code really writes."""
    url = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
    record_read(store, "espn_injuries", url, NOW - timedelta(hours=hours))
    store.commit()

    def build(as_of: datetime) -> DailyReport:
        notes = tuple(
            MissingInput(MissingKind.STALE_SOURCE, None, f"{source}: {reason}")
            for source, reason in stale_sources(store, as_of)
        )
        return _report(DailyLine(_row(), _prediction(missing=notes), ""))

    first, later = build(NOW), build(NOW + timedelta(hours=5))
    assert report_json(first)["games"] != report_json(later)["games"]
    assert inputs_sha256(first) == inputs_sha256(later)


def test_the_schema_version_changes_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    # A new report shape must publish again, even when no input changed.
    first = inputs_sha256(_report())
    monkeypatch.setattr("hockey_model.daily_report.SCHEMA_VERSION", 2)
    assert inputs_sha256(_report()) != first
