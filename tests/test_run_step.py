"""Tests for scripts/run-step.sh, the progress lines of the shell scripts (#133)."""

import re
import subprocess
from pathlib import Path

import pytest
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "run-step.sh")
# The same line format as src/hockey_model/progress.py.
LINE = re.compile(
    r"progress (\S+) (\S+) (start|tick|stop|end) elapsed=([0-9.]+)s"
    r"( done=[0-9]+(/[0-9]+)?)?( budget_left=[0-9]+s)?( reason=.+)?"
)


def _bash(body: str) -> subprocess.CompletedProcess[str]:
    script = f'set -euo pipefail\nsource "{SCRIPT}"\nRUN_STEP_COMMAND=demo\n{body}\n'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, timeout=60
    )


def test_a_step_logs_its_start_and_end_and_keeps_the_output_of_the_command() -> None:
    result = _bash("run_step greet echo hello")
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    start, end = result.stderr.splitlines()
    assert start == "progress demo greet start elapsed=0.0s"
    assert LINE.fullmatch(end)
    assert end.startswith("progress demo greet end elapsed=")


def test_a_failed_step_logs_a_stop_line_and_stops_a_script_under_set_e() -> None:
    result = _bash("run_step bad bash -c 'exit 3'\necho after")
    assert result.returncode == 3
    assert "after" not in result.stdout
    last = result.stderr.splitlines()[-1]
    assert LINE.fullmatch(last)
    assert " stop elapsed=" in last
    assert last.endswith(" reason=exit code 3")


def test_a_caller_can_read_the_exit_code_of_a_failed_step() -> None:
    result = _bash("rc=0\nrun_step bad bash -c 'exit 4' || rc=$?\necho \"rc=$rc\"")
    assert result.stdout == "rc=4\n"


def test_the_redirect_of_the_caller_reaches_only_the_command() -> None:
    result = _bash("run_step greet echo hello >&2")
    assert result.stdout == ""
    assert "hello" in result.stderr


@pytest.mark.parametrize("name", ["", "two words"])
def test_a_step_name_that_is_not_one_word_is_refused(name: str) -> None:
    result = _bash(f"run_step '{name}' echo hi")
    assert result.returncode == 2
    assert "must be one word" in result.stderr
    assert "hi" not in result.stdout


def test_a_missing_script_name_is_refused() -> None:
    script = f'set -euo pipefail\nsource "{SCRIPT}"\nrun_step one echo hi\n'
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 2
    assert "RUN_STEP_COMMAND" in result.stderr
