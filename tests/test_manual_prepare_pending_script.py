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


def _runs(*runs: tuple[str, str] | tuple[str, str, str]) -> str:
    """Return a run list payload. Each tuple is the workflow path, the event, and the branch."""
    rows = [
        {
            "id": 1000 + n,
            "path": run[0],
            "event": run[1],
            "head_branch": run[2] if len(run) > 2 else "main",
        }
        for n, run in enumerate(runs)
    ]
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


def _run_with(
    fakes: Path, tmp_path: Path, args: tuple[str, ...], runs: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=tmp_path,
        env=_env(fakes, tmp_path, FAKE_RUNS_JSON=runs),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("branch", "expected"),
    [("main", "yes\n"), ("feat/x", "no\n")],
)
def test_the_branch_argument_keeps_only_the_runs_of_that_branch(
    fakes: Path, tmp_path: Path, branch: str, expected: str
) -> None:
    """A manual run on a feature branch must not make the scheduled run of main stand aside."""
    runs = _runs((_MUTATION, "workflow_dispatch", branch))
    result = _run_with(fakes, tmp_path, ("mutation.yml", "--branch", "main"), runs)
    assert (result.returncode, result.stdout) == (0, expected)


def test_without_a_branch_argument_a_run_of_any_branch_counts(fakes: Path, tmp_path: Path) -> None:
    """The store group is shared by every branch, so the prepare answer reads all of them."""
    runs = _runs((_PREPARE, "workflow_dispatch", "feat/x"))
    result = _run_with(fakes, tmp_path, (), runs)
    assert (result.returncode, result.stdout) == (0, "yes\n")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--branch",), "--branch needs a branch name"),
        (("--branch", ""), "--branch needs a branch name"),
        (("--branch", "main", "--branch", "x"), "only one --branch is allowed"),
    ],
)
def test_a_bad_branch_argument_is_refused(
    fakes: Path, tmp_path: Path, args: tuple[str, ...], message: str
) -> None:
    result = _run_with(fakes, tmp_path, args, _runs())
    assert result.returncode == 2
    assert message in result.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("mutation.yml", "other.yml"), "only one workflow name"),
        # The default twice is two names too, and a guard on the value lets it through.
        (("daily-prepare.yml", "daily-prepare.yml"), "only one workflow name"),
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
