"""The stand-aside decision of scripts/prepare-stand-aside.sh (#335).

The script is copied beside a stub of manual-prepare-pending.sh, so each test sets the answer
that the lookup gives.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "prepare-stand-aside.sh")


def _run(
    tmp_path: Path, stub_body: str, event: str = "schedule", summary: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run the script with a lookup stub that has ``stub_body``; return the result."""
    folder = tmp_path / "scripts"
    folder.mkdir()
    shutil.copy(SCRIPT, folder / "prepare-stand-aside.sh")
    stub = folder / "manual-prepare-pending.sh"
    stub.write_text("#!/usr/bin/env bash\n" + stub_body)
    stub.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "EVENT": event, "GITHUB_OUTPUT": str(tmp_path / "out")}
    if summary:
        env["GITHUB_STEP_SUMMARY"] = str(tmp_path / "summary")
    return subprocess.run(
        ["bash", str(folder / "prepare-stand-aside.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _output(tmp_path: Path) -> str:
    out = tmp_path / "out"
    return out.read_text(encoding="utf-8") if out.exists() else ""


def test_a_manual_run_goes_on_without_asking_the_lookup(tmp_path: Path) -> None:
    result = _run(tmp_path, "exit 9\n", event="workflow_dispatch")
    assert result.returncode == 0, result.stderr
    assert _output(tmp_path) == "go=true\n"


def test_a_scheduled_run_goes_on_when_no_manual_run_waits(tmp_path: Path) -> None:
    result = _run(tmp_path, "echo no\n")
    assert result.returncode == 0, result.stderr
    assert _output(tmp_path) == "go=true\n"


def test_a_scheduled_run_stands_aside_when_a_manual_run_waits(tmp_path: Path) -> None:
    result = _run(tmp_path, "echo yes\n", summary=True)
    assert result.returncode == 0, result.stderr
    assert "::warning::A manual daily-prepare run waits or runs" in result.stderr
    assert _output(tmp_path) == "go=false\n"
    assert "stood aside" in (tmp_path / "summary").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "stub_body",
    ["echo maybe\n", "echo\n", "exit 3\n"],
    ids=["unknown-answer", "empty-answer", "lookup-failed"],
)
def test_a_failed_or_unknown_lookup_stops_and_never_writes_go(
    tmp_path: Path, stub_body: str
) -> None:
    """A failure must not read as no pending run, so no go value is written (#335)."""
    result = _run(tmp_path, stub_body)
    assert result.returncode == 1
    assert _output(tmp_path) == ""


def test_an_unknown_argument_exits_2(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "unknown argument" in result.stderr
