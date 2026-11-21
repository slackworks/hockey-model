"""The backtest with worker processes gives the same bytes as the serial backtest."""

import json
import math
import random
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_runner_store import PLAN, seed

from hockey_model.backtest import runner
from hockey_model.backtest.game_records import write_game_records
from hockey_model.backtest.runner import (
    PARALLEL_TARGET_S,
    PARALLEL_TARGET_WORKERS,
    merge_scored,
    run_backtest,
)
from hockey_model.game_model.features import FEATURES, FeatureRow
from hockey_model.game_model.training import TrainingRow
from hockey_model.sources.nhl import GameRow
from hockey_model.types import HockeyModelError


def _outputs(report: runner.BacktestReport, records: list, tmp_path: Path, name: str):
    csv_path = tmp_path / f"{name}.csv"
    write_game_records(csv_path, records)
    return json.dumps(asdict(report), sort_keys=True), csv_path.read_bytes()


def test_workers_give_a_byte_identical_report_and_csv(
    store: sqlite3.Connection, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    seed(store, range(1, 8))
    store.commit()
    serial = _outputs(*run_backtest(store, PLAN, candidates=FEATURES), tmp_path, "serial")
    capfd.readouterr()
    parallel = _outputs(
        *run_backtest(store, PLAN, candidates=FEATURES, workers=3), tmp_path, "parallel"
    )
    assert parallel == serial
    log = capfd.readouterr().err
    for unit in ("build actual rows 2", "build guess rows 7", "validate elo", "score model_actual"):
        assert f"backtest: unit {unit} took " in log
    assert "backtest: total " in log
    assert "with 3 worker(s)" in log
    assert "target" not in log


def test_a_run_for_one_worker_logs_the_same_units(
    store: sqlite3.Connection, capfd: pytest.CaptureFixture[str]
) -> None:
    seed(store, range(1, 8))
    store.commit()
    run_backtest(store, PLAN, candidates=FEATURES)
    log = capfd.readouterr().err
    assert "backtest: unit walk-forward fit before season 6 took " in log
    assert "backtest: unit walk-forward fit before season 7 took " in log


def test_workers_need_a_store_file() -> None:
    memory = sqlite3.connect(":memory:")
    try:
        with pytest.raises(HockeyModelError, match="no file"):
            run_backtest(memory, PLAN, candidates=FEATURES, workers=2)
    finally:
        memory.close()


def test_a_selection_error_in_the_parallel_run_is_the_same_error(
    store: sqlite3.Connection,
) -> None:
    store.commit()
    with pytest.raises(HockeyModelError, match="selection seasons"):
        run_backtest(store, PLAN, candidates=FEATURES, workers=2)


def _scored(game_id: int, season: int, minute: int) -> runner._Scored:
    start = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(minutes=minute)
    game = GameRow(game_id, season, 2, start, "TOR", "BOS", "OFF", 3, 2, "REG")
    row = TrainingRow(game, FeatureRow(game_id, 0.0, {}, (), True), True)
    return runner._Scored(row, {})


_KEYS = st.lists(st.tuples(st.integers(1, 4), st.integers(0, 5)), min_size=0, max_size=30)


@given(_KEYS, st.randoms(use_true_random=False))
def test_the_merge_order_does_not_depend_on_the_order_the_units_finished(
    keys: list[tuple[int, int]], shuffler: random.Random
) -> None:
    """Games share seasons and starts, so ties exist. The game ID is unique and breaks them."""
    games = [_scored(game_id, season, minute) for game_id, (season, minute) in enumerate(keys)]
    expected = merge_scored(games)
    shuffled = list(games)
    shuffler.shuffle(shuffled)
    assert merge_scored(shuffled) == expected
    assert merge_scored(expected) == expected
    order = [(g.row.game.season, g.row.game.start, g.row.game.game_id) for g in expected]
    assert order == sorted(order)


def test_a_tie_on_season_and_start_is_broken_by_the_game_id() -> None:
    first, second = _scored(5, 1, 0), _scored(4, 1, 0)
    assert [g.row.game.game_id for g in merge_scored([first, second])] == [4, 5]


_PROBS = st.floats(min_value=0.01, max_value=0.99, allow_nan=False)


@st.composite
def _run_inputs(draw: st.DrawFn) -> tuple[list[float], list[bool], list[float]]:
    size = draw(st.integers(1, 12))
    probs = draw(st.lists(_PROBS, min_size=size, max_size=size))
    wins = draw(st.lists(st.booleans(), min_size=size, max_size=size))
    elo = draw(st.lists(st.floats(0.0, 3.0, allow_nan=False), min_size=size, max_size=size))
    return probs, wins, elo


@given(_run_inputs())
def test_the_elo_run_has_no_interval_and_no_difference(
    inputs: tuple[list[float], list[bool], list[float]],
) -> None:
    probs, wins, elo_losses = inputs
    scored = runner._scored_run(runner.ELO, probs, wins, elo_losses, 0.05)
    assert scored.diff_vs_elo_ci is None
    assert scored.mean_diff_vs_elo is None
    assert scored.games == len(probs)


@given(_run_inputs())
def test_a_model_run_reports_the_mean_difference_from_the_elo(
    inputs: tuple[list[float], list[bool], list[float]],
) -> None:
    probs, wins, elo_losses = inputs
    scored = runner._scored_run(runner.MODEL_ACTUAL, probs, wins, elo_losses, 0.05)
    # Too few games for blocks leave the interval unavailable, and the mean difference stays.
    expected = sum(
        -math.log(p if won else 1.0 - p) - e
        for p, won, e in zip(probs, wins, elo_losses, strict=True)
    ) / len(probs)
    assert scored.mean_diff_vs_elo == pytest.approx(expected)
    assert scored.games == len(probs)


@pytest.mark.parametrize("workers", [0, -1])
def test_a_worker_count_below_one_is_an_error_for_the_library_too(
    store: sqlite3.Connection, workers: int
) -> None:
    with pytest.raises(HockeyModelError, match="workers must be at least 1"):
        run_backtest(store, PLAN, candidates=FEATURES, workers=workers)


def test_the_log_states_the_target_for_the_worker_count_it_was_set_for(
    store: sqlite3.Connection, capfd: pytest.CaptureFixture[str]
) -> None:
    seed(store, range(1, 8))
    store.commit()
    run_backtest(store, PLAN, candidates=FEATURES, workers=PARALLEL_TARGET_WORKERS)
    log = capfd.readouterr().err
    assert f"with {PARALLEL_TARGET_WORKERS} worker(s); the target on the real store is " in log
    assert f"{PARALLEL_TARGET_S}s" in log
