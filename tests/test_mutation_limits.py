"""Tests for scripts/mutation-limits.sh, which sets the shard time limits (#293)."""

import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

SCRIPT = require_path(Path(__file__).resolve().parents[1] / "scripts" / "mutation-limits.sh")
GITHUB_JOB_MAX = 360


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False)


def _limits(*args: str) -> tuple[int, int]:
    result = _run(*args)
    assert result.returncode == 0, result.stderr
    fields = dict(line.split("=") for line in result.stdout.splitlines())
    assert set(fields) == {"step_minutes", "job_minutes"}
    return int(fields["step_minutes"]), int(fields["job_minutes"])


@pytest.mark.parametrize("args", [(), ("",)])
def test_no_value_gives_the_scheduled_limits(args: tuple[str, ...]) -> None:
    assert _limits(*args) == (25, 30)


def test_a_value_sets_the_step_limit_and_leaves_room_for_setup_and_upload() -> None:
    assert _limits("150") == (150, 165)


@pytest.mark.parametrize("value", ["1", "345"])
def test_the_ends_of_the_range_are_valid(value: str) -> None:
    step, job = _limits(value)
    assert step == int(value)
    assert job <= GITHUB_JOB_MAX


@pytest.mark.parametrize(
    "value",
    ["0", "-5", " ", " 30", "30 ", "1h", "abc", "25.5", "+30", "030", "346", "1000", "null"],
)
def test_a_value_that_is_not_a_whole_number_in_range_is_refused(value: str) -> None:
    result = _run(value)
    assert result.returncode == 2
    assert result.stdout == ""
    assert f"got '{value}'" in result.stderr
    assert "from 1 to 345" in result.stderr


def test_two_arguments_are_refused() -> None:
    result = _run("10", "20")
    assert result.returncode == 2
    assert "at most one argument" in result.stderr


def test_the_help_text_names_the_usage() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "Usage: mutation-limits.sh [STEP_MINUTES]" in result.stdout


@given(st.integers(min_value=1, max_value=345))
def test_a_job_always_outlasts_its_step_and_fits_in_github_limit(minutes: int) -> None:
    step, job = _limits(str(minutes))
    assert step == minutes
    assert step < job <= GITHUB_JOB_MAX
