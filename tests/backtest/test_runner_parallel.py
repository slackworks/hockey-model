"""The backtest with worker processes gives the same bytes as the serial backtest."""

import json
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
from hockey_model.backtest.runner import PARALLEL_TARGET_S, merge_scored, run_backtest
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
    for unit in ("build actual rows", "build guess rows", "validate elo", "score model_actual"):
        assert f"backtest: unit {unit} took " in log
    assert "backtest: total " in log
    assert "with 3 worker(s)" in log
    assert f"{PARALLEL_TARGET_S}s on the real store" in log


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


@given(st.randoms(use_true_random=False))
def test_the_merge_order_does_not_depend_on_the_order_the_units_finished(
    shuffler: random.Random,
) -> None:
    games = [_scored(i, 1 + i // 5, i * 3 % 7) for i in range(20)]
    expected = merge_scored(games)
    shuffled = list(games)
    shuffler.shuffle(shuffled)
    assert merge_scored(shuffled) == expected
    keys = [(g.row.game.season, g.row.game.start, g.row.game.game_id) for g in expected]
    assert keys == sorted(keys)


def test_a_tie_on_season_and_start_is_broken_by_the_game_id() -> None:
    first, second = _scored(5, 1, 0), _scored(4, 1, 0)
    assert [g.row.game.game_id for g in merge_scored([first, second])] == [4, 5]
