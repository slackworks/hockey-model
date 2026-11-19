"""Tests for the lookup that names the prepare run holding the store group."""

import json
from pathlib import Path

import pytest
from report_script_support import _env, _run

DAY = "2026-11-04"
_PREPARE = ".github/workflows/daily-prepare.yml"
_REPORT = ".github/workflows/daily-report.yml"


def _runs(*runs: tuple[str, str | None]) -> str:
    """Return a workflow run list payload for the given workflow paths and start times."""
    rows = [
        {"path": path} if started is None else {"path": path, "started_at": started}
        for path, started in runs
    ]
    return json.dumps({"workflow_runs": rows})


def test_no_runs_means_no_prepare_holds_the_group(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_running_prepare_names_its_start(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RUNS_JSON=_runs((_PREPARE, "2026-11-04T08:07:12Z")))
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "2026-11-04T08:07:12Z"


def test_the_earliest_of_two_prepares_wins(fakes: Path, tmp_path: Path) -> None:
    # The earlier start holds the group first, so it is the one that the report waits behind.
    env = _env(
        fakes,
        tmp_path,
        FAKE_RUNS_JSON=_runs(
            (_PREPARE, "2026-11-04T09:30:00Z"),
            (_PREPARE, "2026-11-04T08:00:00Z"),
        ),
    )
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "2026-11-04T08:00:00Z"


def test_a_queued_prepare_names_now_as_its_earliest_start(fakes: Path, tmp_path: Path) -> None:
    # A queued run has no start time yet, so this run cannot read it as a short run.
    env = _env(fakes, tmp_path, FAKE_RUNS_JSON=_runs((_PREPARE, None)))
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("Z")
    assert result.stdout != ""


def test_a_report_run_alone_holds_no_store_group(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RUNS_JSON=_runs((_REPORT, "2026-11-04T11:44:00Z")))
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_failed_lookup_is_not_an_empty_answer(fakes: Path, tmp_path: Path) -> None:
    """A broken lookup read as "no prepare" would let the report start behind a live run."""
    env = _env(fakes, tmp_path, FAKE_RUNS_FAIL="1")
    result = _run("prep-running.sh", env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot read the running workflow runs" in result.stderr


def test_a_payload_that_jq_cannot_read_is_not_an_empty_answer(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RUNS_JSON="{not json")
    result = _run("prep-running.sh", env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot read the workflow run list" in result.stderr


def test_an_unknown_argument_is_refused(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--nope")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_help_names_the_output(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "ISO 8601 UTC instant" in result.stdout


@pytest.mark.parametrize("workflow", ["daily-report.yml"])
def test_the_report_workflow_passes_the_prepare_start(workflow: str) -> None:
    """The workflow is the only caller: it holds no lock, so it can read the running runs."""
    from mutation_copy import require_path

    root = Path(__file__).resolve().parents[1]
    text = require_path(root / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    assert "bash scripts/prep-running.sh" in text
    assert "--prep-started" in text
    # The check must run before the gate decides, so the flag reaches report-gate.sh.
    assert text.index("prep-running.sh") < text.index("report-gate.sh")
