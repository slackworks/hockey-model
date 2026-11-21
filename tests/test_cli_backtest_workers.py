"""The backtest command passes its worker count to the runner."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hockey_model import cli
from hockey_model.backtest.game_records import GameRecord
from hockey_model.backtest.runner import BacktestReport
from hockey_model.types import HockeyModelError


def _spy(seen: list[int]) -> object:
    report = BacktestReport((), (), (), (), {}, (), False, (), (), {})
    record = GameRecord(1, datetime(2026, 1, 1, tzinfo=UTC), "TOR", "BOS", "elo", 0.5, True)

    def fake(conn: object, *, candidates: object, workers: int) -> tuple[object, list[object]]:
        seen.append(workers)
        return report, [record]

    return fake


@pytest.mark.parametrize(("args", "expected"), [([], 1), (["--workers", "3"], 3)])
def test_the_worker_count_reaches_the_runner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    args: list[str],
    expected: int,
) -> None:
    seen: list[int] = []
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    monkeypatch.setattr("hockey_model.store_cli.run_backtest", _spy(seen))
    assert cli.main(["backtest", *args]) == 0
    assert json.loads(capsys.readouterr().out)["success"] is False
    assert seen == [expected]


def test_a_worker_count_that_is_not_a_number_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["backtest", "--workers", "many"]) == 2
    assert "--workers" in capsys.readouterr().err


def test_the_worker_count_reaches_experiments_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    seen: list[tuple[str, int]] = []

    def fake(
        record_id: str, conn: object, games_out: object, *, workers: int
    ) -> tuple[dict[str, object], list[object]]:
        seen.append((record_id, workers))
        return {"ok": True}, []

    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    monkeypatch.setattr("hockey_model.experiments.check.run_experiment", fake)
    assert cli.main(["experiments", "run", "0001-x", "--workers", "2"]) == 0
    capsys.readouterr()
    assert seen == [("0001-x", 2)]


@pytest.mark.parametrize(("args", "expected"), [([], 1), (["--workers", "4"], 4)])
def test_the_worker_count_reaches_the_rows_of_fit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    args: list[str],
    expected: int,
) -> None:
    seen: list[int] = []

    def fake(conn: object, first: int, last: int, names: object, workers: int) -> list[object]:
        seen.append(workers)
        raise HockeyModelError("stop after the rows")

    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    monkeypatch.setattr("hockey_model.store_cli.last_trained_season", lambda conn: 20252026)
    monkeypatch.setattr("hockey_model.store_cli.actual_rows", fake)
    assert cli.main(["fit", "--features", "rest", *args]) == 2
    assert "stop after the rows" in capsys.readouterr().err
    assert seen == [expected]


@pytest.mark.parametrize(
    "command",
    [["backtest"], ["fit", "--features", "rest"], ["experiments", "run", "0001-x"]],
    ids=["backtest", "fit", "experiments_run"],
)
@pytest.mark.parametrize("workers", ["0", "-3"])
def test_a_worker_count_below_one_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], command: list[str], workers: str
) -> None:
    assert cli.main([*command, "--workers", workers]) == 2
    assert "must be 1 or more" in capsys.readouterr().err
