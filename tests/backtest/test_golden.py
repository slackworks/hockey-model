"""The golden check: a speed change must move no number of the backtest on the seeded store.

A change that moves a number on purpose is a model change. Run this module with
UPDATE_GOLDEN=1 to rewrite tests/fixtures/backtest_golden.json, and commit that file in its own
commit with the reason.
"""

import importlib.util
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from mutation_copy import require_path
from test_runner_store import PLAN, seed

from hockey_model.backtest.game_records import GameRecord
from hockey_model.backtest.golden import canonical_hashes, write_golden_files
from hockey_model.backtest.runner import run_backtest
from hockey_model.game_model.features import FEATURES

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "backtest_golden.json"
SCRIPT = require_path(ROOT / "scripts" / "backtest_golden.py")


def _canonical(store: sqlite3.Connection, workers: int = 1) -> dict[str, str]:
    seed(store, range(1, 8))
    store.commit()
    report, records = run_backtest(store, PLAN, candidates=FEATURES, workers=workers)
    return canonical_hashes(report, records)


def test_the_seeded_backtest_matches_the_committed_hashes(store: sqlite3.Connection) -> None:
    hashes = _canonical(store)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        FIXTURE.write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert hashes == committed, (
        "a number of the backtest moved; if the change is a model change, run this test with "
        "UPDATE_GOLDEN=1 and commit tests/fixtures/backtest_golden.json with the reason"
    )


def test_workers_do_not_move_the_committed_hashes(store: sqlite3.Connection) -> None:
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert _canonical(store, workers=2) == committed


def _with_probability(records: list[GameRecord], value: float) -> list[GameRecord]:
    return [replace(records[0], home_win_prob=value), *records[1:]]


def test_a_moved_number_changes_the_hashes(store: sqlite3.Connection) -> None:
    seed(store, range(1, 8))
    store.commit()
    report, records = run_backtest(store, PLAN, candidates=FEATURES)
    moved = _with_probability(records, records[0].home_win_prob + 0.001)
    assert canonical_hashes(report, moved) != canonical_hashes(report, records)


def test_a_last_bit_difference_does_not_change_the_canonical_hashes(
    store: sqlite3.Connection,
) -> None:
    seed(store, range(1, 8))
    store.commit()
    report, records = run_backtest(store, PLAN, candidates=FEATURES)
    nudged = _with_probability(records, records[0].home_win_prob + 1e-14)
    assert canonical_hashes(report, nudged) == canonical_hashes(report, records)


def test_the_files_and_the_hashes_are_written(store: sqlite3.Connection, tmp_path: Path) -> None:
    seed(store, range(1, 8))
    store.commit()
    report, records = run_backtest(store, PLAN, candidates=FEATURES)
    out = tmp_path / "golden"
    hashes = write_golden_files(report, records, out)
    assert sorted(p.name for p in out.iterdir()) == ["games.csv", "hashes.json", "report.json"]
    assert json.loads((out / "hashes.json").read_text(encoding="utf-8")) == hashes
    assert hashes["canonical"] == json.loads(FIXTURE.read_text(encoding="utf-8"))
    again = write_golden_files(report, records, tmp_path / "again")
    assert again["exact"] == hashes["exact"]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backtest_golden", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_names_a_missing_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _script().main(["--db", str(tmp_path / "absent.db"), "--out", str(tmp_path / "out")])
    assert code == 2
    assert "no store file at" in capsys.readouterr().err


def test_the_script_reports_a_backtest_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    code = _script().main(["--db", str(empty), "--out", str(tmp_path / "out")])
    assert code != 0
    assert capsys.readouterr().err
