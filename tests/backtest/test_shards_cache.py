"""The sharded build reads the rows of a season from the cache when its key has not changed."""

import contextlib
import multiprocessing
import os
import sqlite3
from collections.abc import Generator, Sequence
from pathlib import Path

import pytest
from test_runner_store import PLAN, seed

from hockey_model.backtest import shards
from hockey_model.backtest.row_cache import cache_dir
from hockey_model.backtest.row_codec import decode_rows, encode_rows
from hockey_model.backtest.shards import actual_rows, build_sharded
from hockey_model.backtest.units import database_path
from hockey_model.game_model.features import FEATURES, GoalieMode
from hockey_model.game_model.training import training_rows
from hockey_model.types import HockeyModelError

SEASONS = range(PLAN.fit_first, PLAN.scored[1] + 1)


@pytest.fixture
def seeded(store: sqlite3.Connection) -> sqlite3.Connection:
    seed(store, range(1, 8))
    store.commit()
    return store


def _directory(store: sqlite3.Connection) -> Path:
    return cache_dir(database_path(store))


def _names(store: sqlite3.Connection) -> set[str]:
    directory = _directory(store)
    return {p.name for p in directory.iterdir()} if directory.exists() else set()


def _full(store: sqlite3.Connection, mode: GoalieMode) -> list:
    return training_rows(store, PLAN.fit_first, PLAN.scored[1], mode, PLAN.burn_in, names=FEATURES)


def test_a_first_build_writes_one_file_for_each_season_and_mode(
    seeded: sqlite3.Connection,
) -> None:
    actual, guess = build_sharded(seeded, PLAN, FEATURES, 2)
    assert actual == _full(seeded, GoalieMode.ACTUAL)
    assert guess == _full(seeded, GoalieMode.GUESS)
    assert len(_names(seeded)) == 2 * len(SEASONS)


def test_a_second_build_gives_the_same_rows_and_rewrites_no_file(
    seeded: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    cold = build_sharded(seeded, PLAN, FEATURES, 2)
    files = {p: p.read_text() for p in _directory(seeded).iterdir()}
    capsys.readouterr()
    warm = build_sharded(seeded, PLAN, FEATURES, 2)
    assert warm == cold
    assert files == {p: p.read_text() for p in _directory(seeded).iterdir()}
    err = capsys.readouterr().err
    assert f"{2 * len(SEASONS)} read from the cache, 0 to build" in err


def test_a_season_is_read_from_its_file(seeded: sqlite3.Connection) -> None:
    """A file with another value shows up in the result, so the file was read."""
    build_sharded(seeded, PLAN, FEATURES, 2)
    (path,) = [p for p in _directory(seeded).iterdir() if p.name.startswith("actual-4-")]
    rows = decode_rows(path.read_text())
    flipped = [rows[0].__class__(rows[0].game, rows[0].features, not rows[0].home_won), *rows[1:]]
    path.write_text(encode_rows(flipped))
    actual, _ = build_sharded(seeded, PLAN, FEATURES, 2)
    first = next(r for r in actual if r.game.season == 4)
    assert first.home_won is flipped[0].home_won


def test_a_new_game_of_the_newest_season_builds_that_season_only(
    seeded: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    build_sharded(seeded, PLAN, FEATURES, 2)
    before = _names(seeded)
    seeded.execute("UPDATE games SET home_score = home_score + 3 WHERE season = 7")
    seeded.commit()
    capsys.readouterr()
    actual, guess = build_sharded(seeded, PLAN, FEATURES, 2)
    assert f"{2 * len(SEASONS) - 2} read from the cache, 2 to build" in capsys.readouterr().err
    after = _names(seeded)
    assert {n for n in before - after} == {n for n in before if "-7-" in n}
    assert len(after) == len(before)
    assert actual == _full(seeded, GoalieMode.ACTUAL)
    assert guess == _full(seeded, GoalieMode.GUESS)


def test_a_changed_old_game_builds_its_season_and_every_later_one(
    seeded: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    build_sharded(seeded, PLAN, FEATURES, 2)
    seeded.execute("UPDATE games SET home_score = home_score + 3 WHERE season = 4")
    seeded.commit()
    capsys.readouterr()
    actual, guess = build_sharded(seeded, PLAN, FEATURES, 2)
    built = 2 * len([s for s in SEASONS if s >= 4])
    assert f"{2 * len(SEASONS) - built} read from the cache, {built} to build" in (
        capsys.readouterr().err
    )
    assert actual == _full(seeded, GoalieMode.ACTUAL)
    assert guess == _full(seeded, GoalieMode.GUESS)


@pytest.mark.parametrize("workers", [1, 2])
def test_a_fit_reads_the_actual_rows_that_a_backtest_wrote(
    seeded: sqlite3.Connection, workers: int
) -> None:
    build_sharded(seeded, PLAN, FEATURES, 2)
    before = _names(seeded)
    rows = actual_rows(seeded, PLAN.fit_first, PLAN.scored[1], FEATURES, workers, PLAN.burn_in)
    assert rows == _full(seeded, GoalieMode.ACTUAL)
    assert _names(seeded) == before


def test_other_features_do_not_read_the_rows_of_these_features(seeded: sqlite3.Connection) -> None:
    build_sharded(seeded, PLAN, FEATURES, 2)
    names = ("xg_form", "rest")
    rows = actual_rows(seeded, PLAN.fit_first, PLAN.scored[1], names, 2, PLAN.burn_in)
    expected = training_rows(
        seeded, PLAN.fit_first, PLAN.scored[1], GoalieMode.ACTUAL, PLAN.burn_in, names=names
    )
    assert rows == expected
    assert len(_names(seeded)) == 2 * len(SEASONS) + len(SEASONS)


def test_a_cache_directory_that_cannot_be_used_does_not_stop_the_build(
    seeded: sqlite3.Connection,
) -> None:
    _directory(seeded).write_text("a file where the directory should be")
    actual, guess = build_sharded(seeded, PLAN, FEATURES, 2)
    assert actual == _full(seeded, GoalieMode.ACTUAL)
    assert guess == _full(seeded, GoalieMode.GUESS)


def test_a_damaged_file_is_built_again_and_written_again(seeded: sqlite3.Connection) -> None:
    build_sharded(seeded, PLAN, FEATURES, 2)
    (path,) = [p for p in _directory(seeded).iterdir() if p.name.startswith("guess-3-")]
    path.write_text("not json")
    _, guess = build_sharded(seeded, PLAN, FEATURES, 2)
    assert guess == _full(seeded, GoalieMode.GUESS)
    assert decode_rows(path.read_text())


def test_a_file_with_no_rows_for_a_season_that_has_games_is_built_again(
    seeded: sqlite3.Connection,
) -> None:
    build_sharded(seeded, PLAN, FEATURES, 2)
    (path,) = [p for p in _directory(seeded).iterdir() if p.name.startswith("actual-5-")]
    path.write_text("[]")
    actual, _ = build_sharded(seeded, PLAN, FEATURES, 2)
    assert actual == _full(seeded, GoalieMode.ACTUAL)
    assert decode_rows(path.read_text())


def _change_scores_when_the_keys_are_made(
    store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, *, after: bool
) -> None:
    """Make the keys, and change the stored scores before or after that, as a refresh would."""
    real = shards.season_keys
    # A refresh writes in the process that runs it. A forked worker calls this function again,
    # and a write from a worker changes the store during the run (#316).
    parent_pid = os.getpid()

    def keys(conn: sqlite3.Connection, burn_in: int, names: Sequence[str]) -> dict[int, str]:
        mine = os.getpid() == parent_pid
        if mine and not after:
            store.execute("UPDATE games SET home_score = home_score + 3 WHERE season = 4")
            store.commit()
        made = real(conn, burn_in, names)
        if mine and after:
            store.execute("UPDATE games SET home_score = home_score + 3 WHERE season = 4")
            store.commit()
        return made

    monkeypatch.setattr(shards, "season_keys", keys)


def test_a_change_before_the_keys_are_made_gives_the_rows_of_the_changed_store(
    seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _change_scores_when_the_keys_are_made(seeded, monkeypatch, after=False)
    actual, guess = build_sharded(seeded, PLAN, FEATURES, 2)
    assert actual == _full(seeded, GoalieMode.ACTUAL)
    assert guess == _full(seeded, GoalieMode.GUESS)


def test_a_change_after_the_keys_are_made_stops_the_workers(
    seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _change_scores_when_the_keys_are_made(seeded, monkeypatch, after=True)
    with pytest.raises(HockeyModelError, match="the store changed during the run"):
        build_sharded(seeded, PLAN, FEATURES, 2)
    # The seasons before the change keep their keys, so only they may write a file.
    assert {int(n.split("-")[1]) for n in _names(seeded)} <= {2, 3}


def test_the_fork_helper_puts_the_default_start_method_back() -> None:
    """A worker that keeps spawn changes every later test in the same worker.

    CI on Linux runs the default method, which is forkserver. A helper that puts spawn back
    after a fork test hides a difference between that test and CI.
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("this platform has no fork start method")
    previous = multiprocessing.get_start_method(allow_none=True)
    # A reset context answers None, which is the case that the helper got wrong.
    multiprocessing.set_start_method(None, force=True)
    try:
        with _fork_start_method():
            assert multiprocessing.get_start_method() == "fork"
        assert multiprocessing.get_start_method(allow_none=True) is None
    finally:
        multiprocessing.set_start_method(previous or "spawn", force=True)


@contextlib.contextmanager
def _fork_start_method() -> Generator[None]:
    """Run the pool under fork, as a mutation run does, then put the previous method back."""
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("this platform has no fork start method")
    previous = multiprocessing.get_start_method(allow_none=True)
    multiprocessing.set_start_method("fork", force=True)
    try:
        yield
    finally:
        # None with force resets the context to the platform default, so a later test in the
        # same worker runs under the method that CI uses.
        multiprocessing.set_start_method(previous, force=True)


# The test forks on purpose, as a mutation run does. A pytest-xdist worker holds its own
# threads, so Python warns about a fork of a process with more than one thread.
@pytest.mark.filterwarnings(
    r"ignore:This process \(pid=\d+\) is multi-threaded, use of fork\(\):DeprecationWarning"
)
def test_a_change_before_the_keys_are_made_gives_the_rows_under_fork(
    seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the fork case (#316). A forked worker inherits the open connection of its parent, so
    a write from the helper would run again inside a worker and change the store during the
    run. Only the parent writes, and the build gives the rows of the changed store."""
    _change_scores_when_the_keys_are_made(seeded, monkeypatch, after=False)
    with _fork_start_method():
        actual, guess = build_sharded(seeded, PLAN, FEATURES, 2)
    assert actual == _full(seeded, GoalieMode.ACTUAL)
    assert guess == _full(seeded, GoalieMode.GUESS)
