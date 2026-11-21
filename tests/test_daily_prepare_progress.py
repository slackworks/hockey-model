"""Tests for the progress lines of scripts/daily-prepare.sh (#133)."""

from pathlib import Path

from mutation_copy import require_path
from report_script_support import DAY, _env, _run, store_path

require_path(Path(__file__).resolve().parents[1] / "scripts")


def test_prepare_logs_the_start_and_end_of_every_step_on_stderr_only(
    fakes: Path, tmp_path: Path
) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(store_path(tmp_path)))
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert "progress " not in result.stdout
    starts = [line for line in result.stderr.splitlines() if " start elapsed=0.0s" in line]
    assert [line.split()[2] for line in starts] == ["upgrade", "refresh", "backtest", "fit"]
    assert all(line.startswith("progress daily-prepare ") for line in starts)
    for step in ("upgrade", "refresh", "backtest", "fit"):
        assert f"progress daily-prepare {step} end elapsed=" in result.stderr


def test_prepare_names_the_failed_step_with_its_exit_code(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(store_path(tmp_path)), FAKE_BACKTEST_FAIL="1")
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 4
    assert "progress daily-prepare backtest stop elapsed=" in result.stderr
    assert "reason=exit code 4" in result.stderr
    assert "progress daily-prepare fit start" not in result.stderr
