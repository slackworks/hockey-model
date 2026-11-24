"""One worker reads the season row cache when the store is a committed file (#305)."""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest
from test_runner_store import PLAN, seed

from hockey_model.backtest.row_cache import cache_dir
from hockey_model.backtest.runner import run_backtest
from hockey_model.backtest.shards import can_shard
from hockey_model.backtest.units import database_path
from hockey_model.game_model.features import FEATURES
from hockey_model.store import db as store_db

SEASONS = range(PLAN.fit_first, PLAN.scored[1] + 1)
LAST = PLAN.scored[1]


@pytest.fixture
def seeded(store: sqlite3.Connection) -> sqlite3.Connection:
    seed(store, range(1, 8))
    store.commit()
    return store


def _report(conn: sqlite3.Connection, workers: int) -> str:
    report, records = run_backtest(conn, PLAN, candidates=FEATURES, workers=workers)
    return json.dumps([asdict(report), [asdict(r) for r in records]], sort_keys=True, default=str)


def test_a_second_backtest_with_one_worker_reads_every_season_from_the_cache(
    seeded: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _report(seeded, 1)
    assert f"{2 * len(SEASONS)} to build" in capsys.readouterr().err
    assert _report(seeded, 1) == first
    assert f"{2 * len(SEASONS)} read from the cache, 0 to build" in capsys.readouterr().err


def test_one_worker_and_several_workers_share_the_cache_and_give_one_report(
    seeded: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    several = _report(seeded, 3)
    capsys.readouterr()
    assert _report(seeded, 1) == several
    assert f"{2 * len(SEASONS)} read from the cache, 0 to build" in capsys.readouterr().err


def test_a_memory_store_with_one_worker_builds_in_process_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    memory = sqlite3.connect(":memory:")
    for name in ("schema.sql", "views.sql"):
        memory.executescript(Path(store_db.__file__).with_name(name).read_text())
    try:
        seed(memory, range(1, 8))
        memory.commit()
        _, records = run_backtest(memory, PLAN, candidates=FEATURES, workers=1)
    finally:
        memory.close()
    assert records
    assert list(tmp_path.iterdir()) == []


def test_uncommitted_rows_with_one_worker_are_used_and_no_cache_is_written(
    store: sqlite3.Connection,
) -> None:
    seed(store, range(1, 8))
    assert store.in_transaction
    _, records = run_backtest(store, PLAN, candidates=FEATURES, workers=1)
    assert records
    store.commit()
    assert not cache_dir(database_path(store)).exists()


def test_more_than_one_worker_always_shards_and_a_missing_file_then_fails_by_name() -> None:
    memory = sqlite3.connect(":memory:")
    try:
        assert can_shard(memory, 2)
    finally:
        memory.close()


def test_one_worker_shards_only_a_committed_store_file(
    store: sqlite3.Connection,
) -> None:
    seed(store, range(1, 3))
    assert store.in_transaction
    assert not can_shard(store, 1)
    store.commit()
    assert can_shard(store, 1)


def test_one_worker_does_not_shard_a_connection_with_no_file() -> None:
    memory = sqlite3.connect(":memory:")
    try:
        assert not can_shard(memory, 1)
    finally:
        memory.close()
