"""Tests for scripts/report-mutation.sh: the issue calls and the status it prints.

The script runs against fake `gh` and `uv` commands, so no network call and no issue is made.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from report_script_support import REAL_UV

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "report-mutation.sh"
URL = "https://example.test/run/9"
STATS = {"killed": 400, "survived": 10, "timeout": 0, "no_tests": 0, "total": 410}

# The mutation run copies src and tests only, so the script is not there to test.
if "mutants" in Path(__file__).resolve().parts:
    pytest.skip("the mutation copy has no scripts directory", allow_module_level=True)
if not REAL_UV:
    pytest.skip("no uv on PATH, so the script cannot run", allow_module_level=True)


def _run(
    fakes: Path, tmp_path: Path, *args: str, issues: list[dict] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CALLS": str(tmp_path / "calls.log"),
        "FAKE_ISSUES": json.dumps(issues if issues is not None else []),
        # The helper runs under the real uv, so the status is the real decision.
        "FAKE_REAL_UV": REAL_UV,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        # A fake that resolves to itself loops for ever. A timeout fails the test instead.
        timeout=120,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _stats(tmp_path: Path) -> Path:
    path = tmp_path / "stats.json"
    path.write_text(json.dumps(STATS), encoding="utf-8")
    return path


def test_without_file_issue_the_script_makes_no_gh_call(fakes: Path, tmp_path: Path) -> None:
    # A run on another ref still judges its own stats, and writes no issue.
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(_stats(tmp_path)),
        "--execution",
        "success",
        "--run-url",
        URL,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"
    # A branch run judges its stats and stops. It makes no gh call, not even for the title.
    gh_calls = [call for call in _calls(tmp_path) if call.startswith("gh ")]
    assert gh_calls == []
    assert not any("mutation_report.py title" in call for call in _calls(tmp_path))


def test_a_bad_stats_file_fails_a_run_that_files_no_issue(fakes: Path, tmp_path: Path) -> None:
    # A manual run on a branch must not stay green on an interrupted stats file.
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(tmp_path / "absent.json"),
        "--execution",
        "success",
        "--run-url",
        URL,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "execution_failed"


def test_file_issue_creates_the_report_when_none_is_open(fakes: Path, tmp_path: Path) -> None:
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(_stats(tmp_path)),
        "--execution",
        "failure",
        "--run-url",
        URL,
        "--file-issue",
    )
    assert result.returncode == 0, result.stderr
    assert any(call.startswith("gh issue create") for call in _calls(tmp_path))


def test_a_failed_download_names_itself_in_the_report(fakes: Path, tmp_path: Path) -> None:
    # A failed download and a shard that wrote no stats file must not read the same.
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(_stats(tmp_path)),
        "--execution",
        "success",
        "--run-url",
        URL,
        "--download-outcome",
        "failure",
        "--file-issue",
    )
    assert result.returncode == 0, result.stderr
    body = (ROOT / ".tmp" / "mutation-report.md").read_text(encoding="utf-8")
    assert "the artifact download failed" in body


def test_the_issue_search_fails_at_the_list_cap(fakes: Path, tmp_path: Path) -> None:
    # At the cap, an open report can be missing from the list, so a second one would open.
    issues = [{"number": n, "body": None, "author": {"is_bot": True}} for n in range(1000)]
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(_stats(tmp_path)),
        "--execution",
        "failure",
        "--run-url",
        URL,
        "--file-issue",
        issues=issues,
    )
    assert result.returncode == 1
    assert "cap" in result.stderr
    assert not any(call.startswith("gh issue create") for call in _calls(tmp_path))


def test_one_issue_below_the_cap_still_files(fakes: Path, tmp_path: Path) -> None:
    issues = [{"number": n, "body": None, "author": {"is_bot": True}} for n in range(999)]
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(_stats(tmp_path)),
        "--execution",
        "failure",
        "--run-url",
        URL,
        "--file-issue",
        issues=issues,
    )
    assert result.returncode == 0, result.stderr
    assert any(call.startswith("gh issue create") for call in _calls(tmp_path))
