"""Tests for scripts/tlc-plan.sh and the workflow that calls it."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
PLAN = require_path(ROOT / "scripts" / "tlc-plan.sh")
HELPER = require_path(ROOT / "scripts" / "tlc_shards.py")
SHARD_MAP = require_path(ROOT / "specs" / "shards.json")
WORKFLOW = require_path(ROOT / ".github" / "workflows" / "tla.yml")
SHARDS = json.loads(SHARD_MAP.read_text(encoding="utf-8"))["shards"]
NAMES = [shard["name"] for shard in SHARDS]
# The shards that check the daily prepare spec. A change of that code starts these only.
DAILY = [shard["name"] for shard in SHARDS if shard["spec"] == "specs/daily_prepare.tla"]
UPGRADE = [shard["name"] for shard in SHARDS if shard["spec"] == "specs/store_upgrade.tla"]
NULL_SHA = "0" * 40


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=60
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Return a git repository with the helper, the map, and the plan script committed."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "specs").mkdir()
    shutil.copy(PLAN, root / "scripts" / "tlc-plan.sh")
    shutil.copy(HELPER, root / "scripts" / "tlc_shards.py")
    shutil.copy(SHARD_MAP, root / "specs" / "shards.json")
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.test")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _change(repo: Path, relative: str) -> str:
    """Commit a change of one file, and return the commit before it."""
    before = _git(repo, "rev-parse", "HEAD")
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{target.read_text() if target.exists() else ''}x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return before


def _plan(repo: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "scripts" / "tlc-plan.sh")],
        cwd=repo,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_a_manual_run_selects_every_shard(repo: Path) -> None:
    result = _plan(repo, EVENT_NAME="workflow_dispatch")
    assert json.loads(result.stdout) == NAMES
    assert "a manual run" in result.stderr


def test_a_pull_request_that_changes_one_command_selects_no_shard(repo: Path) -> None:
    base = _change(repo, "src/hockey_model/store_cli.py")
    result = _plan(repo, EVENT_NAME="pull_request", BASE_SHA=base)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert "runs no shard" in result.stderr


def test_a_pull_request_that_changes_the_refresh_code_selects_the_daily_prepare_shards(
    repo: Path,
) -> None:
    base = _change(repo, "src/hockey_model/store/refresh_nhl.py")
    result = _plan(repo, EVENT_NAME="pull_request", BASE_SHA=base)
    assert json.loads(result.stdout) == DAILY


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("specs/daily_prepare.tla", DAILY),
        ("specs/store_upgrade.tla", UPGRADE),
        ("specs/store_upgrade.cfg", UPGRADE),
        ("src/hockey_model/store/upgrade.py", UPGRADE),
        ("src/hockey_model/store/schema.sql", UPGRADE),
        ("scripts/tlc.sh", NAMES),
    ],
)
def test_a_pull_request_selects_the_shards_that_cover_the_changed_path(
    repo: Path, path: str, expected: list[str]
) -> None:
    base = _change(repo, path)
    assert json.loads(_plan(repo, EVENT_NAME="pull_request", BASE_SHA=base).stdout) == expected


def test_a_push_compares_with_the_commit_before_it(repo: Path) -> None:
    before = _change(repo, "README.md")
    assert json.loads(_plan(repo, EVENT_NAME="push", BEFORE_SHA=before).stdout) == []


@pytest.mark.parametrize("before", ["", NULL_SHA, "deadbeef" * 5])
def test_a_push_with_no_usable_base_selects_every_shard(repo: Path, before: str) -> None:
    _change(repo, "README.md")
    result = _plan(repo, EVENT_NAME="push", BEFORE_SHA=before)
    assert json.loads(result.stdout) == NAMES


def test_the_result_goes_to_the_output_and_summary_files(repo: Path, tmp_path: Path) -> None:
    output, summary = tmp_path / "out.txt", tmp_path / "summary.md"
    result = _plan(
        repo,
        EVENT_NAME="workflow_dispatch",
        GITHUB_OUTPUT=str(output),
        GITHUB_STEP_SUMMARY=str(summary),
    )
    assert output.read_text(encoding="utf-8") == f"shards={result.stdout.strip()}\n"
    assert "every shard" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("event", ["", "schedule"])
def test_a_missing_or_unknown_event_exits_two(repo: Path, event: str) -> None:
    result = _plan(repo, EVENT_NAME=event)
    assert result.returncode == 2
    assert "pull_request, push, or workflow_dispatch" in result.stderr


def test_help_prints_the_usage(repo: Path) -> None:
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "tlc-plan.sh"), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_the_workflow_calls_the_plan_script_and_keeps_no_path_list() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "bash scripts/tlc-plan.sh" in text
    assert "paths:" not in text, "the path map lives in specs/shards.json only"


def test_the_workflow_runs_one_job_for_each_planned_shard_and_names_it() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "fromJSON(needs.plan.outputs.shards)" in text
    assert "name: tla (${{ matrix.shard }})" in text
    assert 'bash scripts/tlc.sh --shard "$SHARD"' in text
    assert "needs.plan.outputs.shards != '[]'" in text


def test_every_script_that_the_workflow_runs_starts_every_shard_when_it_changes() -> None:
    data = json.loads(SHARD_MAP.read_text(encoding="utf-8"))
    for path in ("scripts/tlc.sh", "scripts/tlc_shards.py", "scripts/tlc-plan.sh"):
        assert path in data["always"], f"{path} is missing from always in specs/shards.json"
    assert ".github/workflows/tla.yml" in data["always"]


def test_a_renamed_covered_file_selects_its_shards(repo: Path) -> None:
    # The old path is the one that the map lists. A diff with renames shows the new path only.
    covered = repo / "src" / "hockey_model" / "store" / "refresh_nhl.py"
    covered.parent.mkdir(parents=True)
    covered.write_text("x = 1\n" * 40, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "src/hockey_model/store/refresh_nhl.py", "src/hockey_model/store/gone.py")
    _git(repo, "commit", "-q", "-m", "rename")
    result = _plan(repo, EVENT_NAME="pull_request", BASE_SHA=base)
    assert json.loads(result.stdout) == DAILY


def test_a_failed_diff_gives_the_reason_of_git(repo: Path) -> None:
    result = _plan(repo, EVENT_NAME="push", BEFORE_SHA="deadbeef" * 5)
    assert json.loads(result.stdout) == NAMES
    assert "the diff against" in result.stderr
    assert "bad object" in result.stderr or "fatal" in result.stderr


@pytest.mark.parametrize(
    "path",
    [
        "src/hockey_model/store/refresh_other.py",
        "src/hockey_model/store/refresh_new_source.py",
        "src/hockey_model/store/nhl_writes.py",
    ],
)
def test_a_module_that_owns_a_modeled_rule_selects_the_daily_prepare_shards(
    repo: Path, path: str
) -> None:
    base = _change(repo, path)
    assert json.loads(_plan(repo, EVENT_NAME="pull_request", BASE_SHA=base).stdout) == DAILY
