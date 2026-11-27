"""Property tests for boxscore revisions, the revisit rule, and the rebuild (#82)."""

import itertools
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from refresh_support import MAIN_GAME, SCORE_TEAMS, FakeClient
from seeding import add_game

from hockey_model.sources.nhl import BOXSCORE_PARSER, BoxscoreRows, parse_boxscore
from hockey_model.store.archive import OK, Fetch, record_observation
from hockey_model.store.boxscore_rebuild import rebuild_boxscores
from hockey_model.store.boxscore_revisions import (
    FIRST,
    LATEST,
    AsOf,
    boxscore_rows,
    derivation_revisions,
    record_revision,
)
from hockey_model.store.boxscore_revisit import _due_at
from hockey_model.store.db import connect
from hockey_model.store.source_reads import BOXSCORES

START = datetime(2026, 1, 10, 18, 0, tzinfo=UTC)
FIRST_READ = datetime(2026, 1, 10, 21, 0, tzinfo=UTC)
PARSER = BOXSCORE_PARSER
HOME, AWAY = SCORE_TEAMS[MAIN_GAME]
# The skater whose goals a timeline changes: the first home forward of the main boxscore.
FIRST_PLAYER = int(
    json.loads(FakeClient._boxscore(MAIN_GAME))["playerByGameStats"]["homeTeam"]["forwards"][0][
        "playerId"
    ]
)
GOALS = st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=6)


def _payload(goals: int) -> bytes:
    """The main boxscore, with the goals of its first skater set to ``goals``."""
    body = json.loads(FakeClient._boxscore(MAIN_GAME))
    skater = body["playerByGameStats"]["homeTeam"]["forwards"][0]
    skater["goals"] = goals
    return json.dumps(body).encode()


def _rows(goals: int) -> BoxscoreRows:
    return parse_boxscore(json.loads(_payload(goals)))


def _runs(values: list[int]) -> list[int]:
    """The value of each run of equal neighbours: the revisions that a timeline must give."""
    return [key for key, _ in itertools.groupby(values)]


@given(
    start_offset=st.integers(min_value=0, max_value=10_000),
    read_offset=st.integers(min_value=-10_000, max_value=40_000),
    days=st.lists(st.integers(min_value=1, max_value=30), min_size=1, max_size=4, unique=True),
)
def test_the_next_revisit_point_is_the_first_one_that_no_read_has_met(
    start_offset: int, read_offset: int, days: list[int]
) -> None:
    start = START + timedelta(minutes=start_offset)
    newest = start + timedelta(minutes=read_offset)
    expected = next(
        (start + timedelta(days=d) for d in sorted(days) if newest < start + timedelta(days=d)),
        None,
    )
    assert _due_at(start, newest, days) == expected


@settings(max_examples=40, deadline=None)
@given(goals=GOALS, gaps=st.lists(st.integers(min_value=1, max_value=50), min_size=6, max_size=6))
def test_a_timeline_of_reads_gives_one_revision_for_each_change(
    goals: list[int], gaps: list[int]
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "store.db")
        try:
            stamps = list(
                itertools.accumulate(
                    (timedelta(hours=g) for g in gaps[: len(goals)]), initial=timedelta()
                )
            )[1:]
            times = [FIRST_READ + s for s in stamps]
            for value, when in zip(goals, times, strict=True):
                obs = record_observation(
                    conn, Fetch(BOXSCORES, "u", when, OK, PARSER, str(MAIN_GAME))
                )
                record_revision(conn, MAIN_GAME, _rows(value), obs)
            expected = _runs(goals)
            revisions = derivation_revisions(conn, MAIN_GAME, 1)
            assert [r.revision for r in revisions] == list(range(1, len(expected) + 1))
            seen = [next(s.goals for s in r.skaters if s.nhl_id == FIRST_PLAYER) for r in revisions]
            assert seen == expected
            first = boxscore_rows(conn, MAIN_GAME, policy=FIRST)
            latest = boxscore_rows(conn, MAIN_GAME, policy=LATEST)
            assert first is not None
            assert latest is not None
            assert (first.revision, latest.revision) == (1, len(expected))
            _check_as_of(conn, revisions, times[0])
        finally:
            conn.close()


def _check_as_of(conn, revisions, origin: datetime) -> None:
    """``AsOf(t)`` returns the newest revision that was fetched at or before ``t``."""
    cutoffs = [origin - timedelta(seconds=1)]
    for revision in revisions:
        cutoffs += [revision.fetched_at, revision.fetched_at + timedelta(seconds=1)]
    for cutoff in cutoffs:
        known = [r for r in revisions if r.fetched_at <= cutoff]
        found = boxscore_rows(conn, MAIN_GAME, policy=AsOf(cutoff))
        if not known:
            assert found is None
        else:
            assert found is not None
            assert found.revision == known[-1].revision
            assert found.skaters == known[-1].skaters
            assert found.goalies == known[-1].goalies


@settings(max_examples=40, deadline=None)
@given(goals=GOALS)
def test_a_repeated_read_adds_no_revision(goals: list[int]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "store.db")
        try:
            for index, value in enumerate(goals):
                when = FIRST_READ + timedelta(hours=index)
                obs = record_observation(
                    conn, Fetch(BOXSCORES, "u", when, OK, PARSER, str(MAIN_GAME))
                )
                record_revision(conn, MAIN_GAME, _rows(value), obs)
                again = record_observation(
                    conn, Fetch(BOXSCORES, "u", when, OK, PARSER, str(MAIN_GAME))
                )
                assert record_revision(conn, MAIN_GAME, _rows(value), again) is None
        finally:
            conn.close()


@settings(max_examples=25, deadline=None)
@given(goals=GOALS)
def test_a_rebuild_gives_one_revision_for_each_change_and_a_second_rebuild_changes_nothing(
    goals: list[int],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "store.db")
        try:
            add_game(conn, MAIN_GAME, START.isoformat(), home=HOME, away=AWAY)
            for index, value in enumerate(goals):
                fetch = Fetch(
                    BOXSCORES,
                    "u",
                    FIRST_READ + timedelta(hours=index),
                    OK,
                    PARSER,
                    str(MAIN_GAME),
                    _payload(value),
                )
                record_observation(conn, fetch)
            first = rebuild_boxscores(conn, PARSER)
            revisions = derivation_revisions(conn, MAIN_GAME, 1)
            assert len(revisions) == len(_runs(goals))
            assert all(a.skaters != b.skaters for a, b in itertools.pairwise(revisions)), (
                "two neighbour revisions hold the same rows"
            )
            assert first.derived == 1
            second = rebuild_boxscores(conn, PARSER)
            assert second.derived == 0
            assert derivation_revisions(conn, MAIN_GAME, 1) == revisions
        finally:
            conn.close()
