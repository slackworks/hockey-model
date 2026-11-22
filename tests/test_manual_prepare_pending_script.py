"""Tests for manual-prepare-pending.sh, with a fake `gh` that answers the run lists."""

import json
import subprocess
from pathlib import Path

import pytest
from mutation_copy import require_path
from report_script_support import _calls, _env

SCRIPT = require_path(Path(__file__).resolve().parents[1] / "scripts" / "manual-prepare-pending.sh")
_PREPARE = ".github/workflows/daily-prepare.yml"
_REPORT = ".github/workflows/daily-report.yml"
_MUTATION = ".github/workflows/mutation.yml"
STATUSES = ["in_progress", "pending", "queued", "waiting", "requested"]


def _runs(*runs: tuple[str, str]) -> str:
    """Return a run list payload. Each pair is the workflow path and the event."""
    rows = [{"id": 1000 + n, "path": path, "event": event} for n, (path, event) in enumerate(runs)]
    return json.dumps({"workflow_runs": rows})


def _run(fakes: Path, tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=_env(fakes, tmp_path, **extra),
        capture_output=True,
        text=True,
        check=False,
    )


def test_no_run_means_no(fakes: Path, tmp_path: Path) -> None:
    result = _run(fakes, tmp_path)
    assert (result.returncode, result.stdout) == (0, "no\n")


@pytest.mark.parametrize("status", STATUSES)
def test_a_manual_prepare_in_any_state_means_yes(fakes: Path, tmp_path: Path, status: str) -> None:
    runs = _runs((_PREPARE, "workflow_dispatch"))
    result = _run(fakes, tmp_path, **{f"FAKE_RUNS_JSON_{status.upper()}": runs})
    assert (result.returncode, result.stdout) == (0, "yes\n")


def test_a_scheduled_prepare_does_not_count(fakes: Path, tmp_path: Path) -> None:
    runs = _runs((_PREPARE, "schedule"))
    result = _run(fakes, tmp_path, FAKE_RUNS_JSON=runs)
    assert (result.returncode, result.stdout) == (0, "no\n")


def test_a_manual_run_of_another_workflow_does_not_count(fakes: Path, tmp_path: Path) -> None:
    runs = _runs((_REPORT, "workflow_dispatch"))
    result = _run(fakes, tmp_path, FAKE_RUNS_JSON=runs)
    assert (result.returncode, result.stdout) == (0, "no\n")


def test_the_workflow_argument_reads_the_named_workflow(fakes: Path, tmp_path: Path) -> None:
    """A scheduled mutation run stands aside for a manual one, so it names its own file (#296)."""
    runs = _runs((_MUTATION, "workflow_dispatch"))
    result = subprocess.run(
        ["bash", str(SCRIPT), "mutation.yml"],
        cwd=tmp_path,
        env=_env(fakes, tmp_path, FAKE_RUNS_JSON=runs),
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout) == (0, "yes\n")


def test_the_workflow_argument_ignores_the_default_workflow(fakes: Path, tmp_path: Path) -> None:
    runs = _runs((_PREPARE, "workflow_dispatch"))
    result = subprocess.run(
        ["bash", str(SCRIPT), "mutation.yml"],
        cwd=tmp_path,
        env=_env(fakes, tmp_path, FAKE_RUNS_JSON=runs),
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout) == (0, "no\n")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("mutation.yml", "other.yml"), "only one workflow name"),
        (("mutation",), "is not a workflow file name"),
        (("extra/daily-prepare.yml",), "is not a workflow file name"),
    ],
)
def test_a_bad_workflow_argument_is_refused(
    fakes: Path, tmp_path: Path, args: tuple[str, ...], message: str
) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=tmp_path,
        env=_env(fakes, tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert message in result.stderr


def test_the_run_list_asks_for_manual_runs_of_each_status(fakes: Path, tmp_path: Path) -> None:
    _run(fakes, tmp_path)
    calls = _calls(tmp_path)
    for status in STATUSES:
        assert any(f"status={status}&event=workflow_dispatch" in call for call in calls)


def test_a_failed_lookup_is_not_read_as_no(fakes: Path, tmp_path: Path) -> None:
    result = _run(fakes, tmp_path, FAKE_RUNS_FAIL="1")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot read the in_progress workflow runs" in result.stderr


def test_a_list_that_is_not_json_is_not_read_as_no(fakes: Path, tmp_path: Path) -> None:
    result = _run(fakes, tmp_path, FAKE_RUNS_JSON="not json")
    assert result.returncode == 2
    assert "cannot read the in_progress workflow run list" in result.stderr


def test_help_prints_the_usage() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.startswith("Say whether a manual daily-prepare run")
    assert "[WORKFLOW]" in result.stdout


def test_an_unknown_argument_is_refused() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--now"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "unknown argument '--now'" in result.stderr
