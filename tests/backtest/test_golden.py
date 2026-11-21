"""The golden check: a speed change must move no number of the backtest on the seeded store.

A change that moves a number on purpose is a model change. Run this module with
UPDATE_GOLDEN=1 to rewrite tests/fixtures/backtest_golden.json. The run then fails on purpose,
so an update never reads as a pass; review the diff and commit the file with the reason.
"""

import json
import os
import runpy
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from mutation_copy import require_path
from test_runner_store import PLAN, seed

from hockey_model.backtest.game_records import GameRecord
from hockey_model.backtest.golden import (
    DECIMALS,
    canonical_fit_hash,
    canonical_hashes,
    report_text,
    rounded,
    write_golden_files,
)
from hockey_model.backtest.runner import BacktestReport, run_backtest
from hockey_model.game_model.features import FEATURES, GoalieMode
from hockey_model.game_model.logistic import LogisticFit, fit_logistic
from hockey_model.game_model.training import training_rows

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "backtest_golden.json"
# The script is outside the mutation copy, so only the tests that run it need the guard.
SCRIPT = ROOT / "scripts" / "backtest_golden.py"
EMPTY_REPORT = BacktestReport((), (), (), (), {}, (), False, (), (), {})
START = datetime(2026, 1, 1, tzinfo=UTC)


def _seeded_run(
    store: sqlite3.Connection, workers: int = 1
) -> tuple[BacktestReport, list[GameRecord]]:
    seed(store, range(1, 8))
    store.commit()
    return run_backtest(store, PLAN, candidates=FEATURES, workers=workers)


def _seeded_fit(store: sqlite3.Connection) -> LogisticFit:
    rows = training_rows(store, 2, 7, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES)
    return fit_logistic([r.features for r in rows], [r.home_won for r in rows], FEATURES)


def _hashes(store: sqlite3.Connection, workers: int = 1) -> dict[str, str]:
    report, records = _seeded_run(store, workers)
    return {
        **canonical_hashes(report, records),
        "canonical_fit_sha256": canonical_fit_hash(_seeded_fit(store)),
    }


def _committed() -> dict[str, str]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_seeded_backtest_matches_the_committed_hashes(store: sqlite3.Connection) -> None:
    hashes, committed = _hashes(store), _committed()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        FIXTURE.write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
        pytest.fail("the golden fixture was rewritten; review the diff and commit it")
    assert hashes == committed, (
        "a number of the backtest moved; if the change is a model change, run this test with "
        "UPDATE_GOLDEN=1 and commit tests/fixtures/backtest_golden.json with the reason"
    )


def test_workers_do_not_move_the_committed_hashes(store: sqlite3.Connection) -> None:
    assert _hashes(store, workers=2) == _committed()


def _with_probability(records: list[GameRecord], value: float) -> list[GameRecord]:
    return [replace(records[0], home_win_prob=value), *records[1:]]


def test_a_moved_number_changes_the_hashes(store: sqlite3.Connection) -> None:
    report, records = _seeded_run(store)
    moved = _with_probability(records, records[0].home_win_prob + 0.001)
    assert canonical_hashes(report, moved) != canonical_hashes(report, records)


def test_a_moved_weight_changes_the_fit_hash(store: sqlite3.Connection) -> None:
    _seeded_run(store)
    fit = _seeded_fit(store)
    moved = replace(fit, intercept=fit.intercept + 0.001)
    assert canonical_fit_hash(moved) != canonical_fit_hash(fit)


def _record(probability: float) -> GameRecord:
    return GameRecord(1, START, "TOR", "BOS", "elo", probability, True)


@given(st.floats(min_value=0.05, max_value=0.95, allow_nan=False), st.floats(0.0, 1e-12))
def test_a_last_bit_difference_does_not_change_the_canonical_hash(
    base: float, noise: float
) -> None:
    scaled = base * 10**DECIMALS
    # A value near a rounding midpoint can cross it, so the property excludes that thin band.
    assume(abs(scaled - round(scaled)) < 0.499)
    same = canonical_hashes(EMPTY_REPORT, [_record(base)])
    assert canonical_hashes(EMPTY_REPORT, [_record(base + noise)]) == same


@given(st.floats(min_value=0.05, max_value=0.95, allow_nan=False), st.floats(1e-7, 1e-2))
def test_a_change_of_a_hundred_millionth_or_more_changes_the_hash(base: float, step: float) -> None:
    assert canonical_hashes(EMPTY_REPORT, [_record(base + step)]) != canonical_hashes(
        EMPTY_REPORT, [_record(base)]
    )


_LEAVES = st.one_of(
    st.none(), st.booleans(), st.integers(), st.text(max_size=5), st.floats(allow_nan=False)
)
_TREES = st.recursive(
    _LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=3), children, max_size=3),
    ),
    max_leaves=12,
)


@given(_TREES)
def test_rounding_twice_is_rounding_once(value: Any) -> None:
    assert rounded(rounded(value)) == rounded(value)


def test_rounding_keeps_the_shape_and_changes_only_floats() -> None:
    value = {"a": (1, 0.123456789123), "b": [True, None, "x", {"c": 2.5}]}
    assert rounded(value) == {"a": [1, 0.123456789], "b": [True, None, "x", {"c": 2.5}]}


def test_the_report_text_is_the_report_as_sorted_json(store: sqlite3.Connection) -> None:
    report, _ = _seeded_run(store)
    text = report_text(report)
    assert text.endswith("\n")
    loaded = json.loads(text)
    assert list(loaded) == sorted(loaded)
    assert loaded["candidate_features"] == list(FEATURES)


def test_the_files_and_the_hashes_are_written(store: sqlite3.Connection, tmp_path: Path) -> None:
    report, records = _seeded_run(store)
    hashes = write_golden_files(report, records, tmp_path / "golden")
    out = tmp_path / "golden"
    assert sorted(p.name for p in out.iterdir()) == ["games.csv", "hashes.json", "report.json"]
    assert json.loads((out / "hashes.json").read_text(encoding="utf-8")) == hashes
    assert sorted(hashes) == [
        "canonical_games_sha256",
        "canonical_report_sha256",
        "decimals",
        "exact_games_sha256",
        "exact_report_sha256",
    ]


def _indent_of_second_line(text: str) -> str:
    line = text.splitlines()[1]
    return line[: len(line) - len(line.lstrip(" "))]


def test_the_report_and_the_hashes_files_are_indented_by_two_spaces(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    report, records = _seeded_run(store)
    write_golden_files(report, records, tmp_path)
    assert _indent_of_second_line(report_text(report)) == "  "
    assert _indent_of_second_line((tmp_path / "hashes.json").read_text(encoding="utf-8")) == "  "
    assert (tmp_path / "hashes.json").read_text(encoding="utf-8").endswith("}\n")


def test_the_out_directory_may_be_new_nested_or_already_there(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    report, records = _seeded_run(store)
    nested = tmp_path / "a" / "b"
    write_golden_files(report, records, nested)
    assert (nested / "report.json").is_file()
    write_golden_files(report, records, nested)
    assert (nested / "hashes.json").is_file()


def test_two_backtest_runs_give_the_same_exact_hashes(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    report, records = _seeded_run(store)
    first = write_golden_files(report, records, tmp_path / "one")
    report_again, records_again = run_backtest(store, PLAN, candidates=FEATURES)
    assert write_golden_files(report_again, records_again, tmp_path / "two") == first


def _run_script(*args: str) -> int:
    return runpy.run_path(str(require_path(SCRIPT)))["main"](list(args))


def _store_path(store: sqlite3.Connection) -> Path:
    store.commit()
    return Path(store.execute("PRAGMA database_list").fetchone()[2])


def test_the_script_names_a_missing_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_script("--db", str(tmp_path / "absent.db"), "--out", str(tmp_path / "o")) == 2
    assert "no store file at" in capsys.readouterr().err


def test_the_script_names_a_backtest_error_without_a_traceback(
    store: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_script("--db", str(_store_path(store)), "--out", str(tmp_path / "o")) == 1
    err = capsys.readouterr().err
    assert err.strip().splitlines()[-1].startswith("backtest_golden: backtest: ")
    assert "Traceback" not in err


def test_the_script_names_an_out_directory_it_cannot_write(
    store: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert _run_script("--db", str(_store_path(store)), "--out", str(blocker / "sub")) == 1
    assert "cannot write to" in capsys.readouterr().err


def test_the_script_does_not_change_the_store(
    store: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store, range(1, 8))
    path = _store_path(store)
    store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.read_bytes()
    # The default plan finds no scored seasons in the made-up store, so the run ends in an error.
    assert _run_script("--db", str(path), "--out", str(tmp_path / "o")) == 1
    capsys.readouterr()
    assert path.read_bytes() == before


def test_the_script_refuses_a_worker_count_below_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run_script("--db", str(tmp_path / "a.db"), "--out", str(tmp_path), "--workers", "0")
    assert excinfo.value.code == 2
    assert "must be 1 or more" in capsys.readouterr().err
