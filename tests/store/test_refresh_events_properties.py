"""Properties of the choice of games for an event read (#35)."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from refresh_support import FakeClient
from seeding import add_game

from hockey_model.store.archive import HTTP_ERROR, OK, Fetch, record_observation
from hockey_model.store.boxscore_revisit import REVISIT_AFTER_DAYS, reads_due
from hockey_model.store.db import connect
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.refresh_events import FIRST_READ_DAYS, games_missing_events
from hockey_model.store.source_reads import BOXSCORES, NHL_PBP, NHL_SHIFTS

ORIGIN = datetime(2026, 1, 10, 18, 0, tzinfo=UTC)
HOURS = st.integers(min_value=0, max_value=24 * 40)
SOURCES = st.sampled_from([BOXSCORES, NHL_PBP, NHL_SHIFTS])
# One game: the hour of its start, and a list of reads as (source, hour offset after start).
GAMES = st.lists(
    st.tuples(HOURS, st.lists(st.tuples(SOURCES, st.integers(0, 24 * 30)), max_size=4)),
    min_size=1,
    max_size=5,
)


def _store(tmp: str, games: list[tuple[int, list[tuple[str, int]]]]):
    conn = connect(Path(tmp) / "store.db")
    for number, (start_hour, reads) in enumerate(games, start=1):
        start = ORIGIN + timedelta(hours=start_hour)
        add_game(conn, number, start.isoformat(), state="OFF")
        for source, offset in reads:
            when = start + timedelta(hours=offset)
            record_observation(conn, Fetch(source, "u", when, OK, "p:1", str(number)))
    return conn


def _expected_due(start: datetime, newest: datetime, now: datetime) -> datetime | None:
    unmet = [
        start + timedelta(days=d)
        for d in sorted(REVISIT_AFTER_DAYS)
        if newest < start + timedelta(days=d)
    ]
    return unmet[0] if unmet and unmet[0] <= now else None


@settings(max_examples=60, deadline=None)
@given(games=GAMES, now_hour=HOURS, source=SOURCES)
def test_the_games_due_are_the_ones_a_model_of_the_revisit_days_names(
    games: list[tuple[int, list[tuple[str, int]]]], now_hour: int, source: str
) -> None:
    now = ORIGIN + timedelta(hours=now_hour)
    expected: list[tuple[datetime, int]] = []
    for number, (start_hour, reads) in enumerate(games, start=1):
        start = ORIGIN + timedelta(hours=start_hour)
        times = [start + timedelta(hours=o) for s, o in reads if s == source]
        point = _expected_due(start, max(times), now) if times else None
        if point is not None:
            expected.append((point, number))
    with tempfile.TemporaryDirectory() as tmp:
        conn = _store(tmp, games)
        try:
            assert reads_due(conn, now, REVISIT_AFTER_DAYS, source) == [
                n for _, n in sorted(expected)
            ]
        finally:
            conn.close()


@settings(max_examples=60, deadline=None)
@given(
    games=GAMES,
    failed=st.lists(st.tuples(st.integers(1, 5), HOURS), max_size=4),
    now_hour=HOURS,
    source=st.sampled_from([NHL_PBP, NHL_SHIFTS]),
)
def test_a_missing_game_is_a_recent_final_game_with_no_good_read(
    games: list[tuple[int, list[tuple[str, int]]]],
    failed: list[tuple[int, int]],
    now_hour: int,
    source: str,
) -> None:
    now = ORIGIN + timedelta(hours=now_hour)
    with tempfile.TemporaryDirectory() as tmp:
        conn = _store(tmp, games)
        try:
            for number, hour in failed:
                when = ORIGIN + timedelta(hours=hour)
                record_observation(conn, Fetch(source, "u", when, HTTP_ERROR, "p:1", str(number)))
            expected = [
                number
                for number, (start_hour, reads) in sorted(
                    enumerate(games, start=1), key=lambda item: item[1][0]
                )
                if ORIGIN + timedelta(hours=start_hour) >= now - timedelta(days=FIRST_READ_DAYS)
                and not any(s == source for s, _ in reads)
            ]
            run = RefreshRun(conn, FakeClient(), now)
            assert sorted(games_missing_events(run, source)) == sorted(expected)
        finally:
            conn.close()
