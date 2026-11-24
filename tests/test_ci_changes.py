"""Tests for scripts/ci-changes.sh and the job gates of ci.yml (#170)."""

import ast
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "ci-changes.sh")
CI = require_path(ROOT / ".github" / "workflows" / "ci.yml")


def decide(paths: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input="".join(f"{p}\n" for p in paths),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md"],
        ["docs/a.md"],
        ["docs/design/deep/nested/note.md", "CHANGELOG.md", "docs/superpowers/plans/p.md"],
    ],
)
def test_a_diff_of_markdown_only_skips_the_code_jobs(paths: list[str]) -> None:
    result = decide(paths)
    assert (result.returncode, result.stdout) == (0, "code=false\n")


@pytest.mark.parametrize(
    "path",
    [
        "src/x.py",
        "docs/x.py",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "docs/design/daily-prepare-tla.md",
        "docs/design/experiment-registry-tla.md",
        "README.md.bak",
        "notes.markdown",
    ],
)
def test_any_other_path_runs_the_code_jobs(path: str) -> None:
    result = decide(["README.md", path])
    assert (result.returncode, result.stdout) == (0, "code=true\n")
    assert path in result.stderr


_READS = {"read_text", "read_bytes", "open", "require_path"}
# A path that ends in one of these names the file itself, as in ``ROOT / "README.md"``.
_ROOT_DOCS = ("README.md", "CHANGELOG.md")


def _names_a_root_doc(node: ast.AST) -> bool:
    """Return True when ``node`` is a constant, or a joined path, that names a root doc."""
    if isinstance(node, ast.JoinedStr | ast.BinOp):
        return any(_names_a_root_doc(child) for child in ast.iter_child_nodes(node))
    return isinstance(node, ast.Constant) and node.value in _ROOT_DOCS


def _call_name(node: ast.Call) -> str:
    """Return the name that a call uses, as in ``read_text`` or ``require_path``."""
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _root_reads(path: Path) -> list[str]:
    """Return each ``README.md`` or ``CHANGELOG.md`` that ``path`` opens through a read call.

    The AST tells a read from a write and from a bare string in test data: only a call to one
    of the names in ``_READS`` counts. Its arguments name the file, and so does the receiver
    of a method call, as in ``(ROOT / "README.md").read_text()``.
    """
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or _call_name(node) not in _READS:
            continue
        values = [*node.args, *(keyword.value for keyword in node.keywords)]
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        if receiver is not None:
            values.append(receiver)
        if any(_names_a_root_doc(value) for value in values):
            found.append(f"{path}:{node.lineno}")
    return found


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ('x = (ROOT / "README.md").read_text()\n', True),
        ('with (ROOT / "CHANGELOG.md").open() as f:\n    f.read()\n', True),
        ('p = require_path(ROOT / "README.md")\n', True),
        ('log = tmp_path / "CHANGELOG.md"\nlog.write_text("x")\n', False),
        ('decide(["README.md"])\n', False),
        ('x = (ROOT / "docs" / "a.md").read_text()\n', False),
    ],
)
def test_the_read_scan_counts_a_read_and_not_a_write(
    tmp_path: Path, source: str, found: bool
) -> None:
    """The scan is only useful if it tells a read from test data and from a write."""
    module = tmp_path / "test_probe.py"
    module.write_text(source, encoding="utf-8")
    assert bool(_root_reads(module)) is found, source


def test_no_test_reads_the_root_readme_or_the_changelog() -> None:
    """A docs-only run skips the tests, so a test that reads these two files would go unrun.

    The skipped job counts as passed, so a docs-only push could leave main red (issue #313).
    Neither file serves a test today: the wheel test reads the README from the built wheel,
    and the release test builds its own changelog. Pin that, so a new read adds the file to
    ``read_by_tests`` in scripts/ci-changes.sh.
    """
    holders = [hit for path in (ROOT / "tests").rglob("*.py") for hit in _root_reads(path)]
    assert holders == [], (
        f"{', '.join(holders)} reads README.md or CHANGELOG.md. A docs-only push skips the "
        "tests, so add the file to read_by_tests in scripts/ci-changes.sh."
    )


def test_every_design_note_that_a_test_reads_runs_the_code_jobs() -> None:
    named = re.compile(r'"docs" / "design" / "([a-z0-9-]+\.md)"')
    notes = {
        f"docs/design/{match}"
        for path in (ROOT / "tests").rglob("*.py")
        for match in named.findall(path.read_text(encoding="utf-8"))
    }
    assert notes
    for note in sorted(notes):
        assert decide([note]).stdout == "code=true\n", note


@pytest.mark.parametrize("paths", [[], [""], ["", ""]])
def test_a_diff_that_lists_no_path_runs_the_code_jobs(paths: list[str]) -> None:
    result = decide(paths)
    assert (result.returncode, result.stdout) == (0, "code=true\n")
    assert "lists no path" in result.stderr


def test_a_last_path_with_no_newline_counts() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)], input="README.md\nsrc/x.py", capture_output=True, text=True
    )
    assert result.stdout == "code=true\n"


_NAMES = st.text(alphabet="abcxyz_-", min_size=1, max_size=6)


@given(
    folders=st.lists(_NAMES, max_size=3),
    stem=_NAMES,
    suffix=st.sampled_from([".md", ".py", ".toml", ".yml", ".tla"]),
)
def test_a_path_is_docs_exactly_when_it_ends_in_md(
    folders: list[str], stem: str, suffix: str
) -> None:
    path = "/".join([*folders, stem + suffix])
    expected = "code=false\n" if suffix == ".md" else "code=true\n"
    assert decide([path]).stdout == expected


def test_a_bad_argument_exits_2_and_help_exits_0() -> None:
    bad = subprocess.run(["bash", str(SCRIPT), "--nope"], capture_output=True, text=True)
    assert bad.returncode == 2
    assert "unknown argument" in bad.stderr
    good = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert good.returncode == 0
    assert "Usage:" in good.stdout


def _job(name: str) -> str:
    text = CI.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", text, re.DOTALL | re.MULTILINE)
    assert match is not None, f"ci.yml has no job named {name}"
    return "".join(
        line
        for line in match.group(1).splitlines(keepends=True)
        if not line.lstrip().startswith("#")
    )


def test_validation_waits_for_the_changes_job_and_its_answer() -> None:
    job = _job("validation")
    assert "needs: changes\n" in job
    assert "if: needs.changes.outputs.code == 'true'" in job


def test_the_test_job_follows_validation_and_skips_with_it() -> None:
    """A skipped validation job skips the test job, so the test job needs no gate of its own."""
    job = _job("test")
    assert "needs: validation\n" in job
    assert "\n    if:" not in job


@pytest.mark.parametrize("name", ["editorconfig", "smoke", "zizmor"])
def test_the_other_jobs_always_run(name: str) -> None:
    job = _job(name)
    assert "needs:" not in job
    assert "\n    if:" not in job


def test_a_push_to_main_runs_every_code_job() -> None:
    job = _job("changes")
    assert '[ "$EVENT" = pull_request ]' in job
    assert 'echo "code=true" >> "$GITHUB_OUTPUT"' in job


def test_the_ci_workflow_has_no_path_filter() -> None:
    """A path filter leaves a required check pending, which blocks the merge."""
    assert not re.search(r"^\s*paths(-ignore)?:", CI.read_text(encoding="utf-8"), re.MULTILINE)


def test_the_ci_test_job_runs_pytest_in_parallel() -> None:
    assert "- run: uv run pytest -n auto\n" in _job("test")


def test_the_pre_push_coverage_gate_runs_pytest_in_parallel() -> None:
    hooks = require_path(ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "rm -f coverage.json && uv run pytest -n auto && " in hooks


def test_the_default_pytest_options_stay_serial() -> None:
    """mutmut runs pytest by itself and needs one process, so addopts holds no -n flag."""
    addopts = re.search(r'addopts = """(.*?)"""', (ROOT / "pyproject.toml").read_text(), re.S)
    assert addopts is not None
    assert not re.search(r"(^|\s)(-n|--numprocesses)\b", addopts.group(1))


def test_the_workflow_lists_the_paths_of_a_rename_on_both_sides() -> None:
    assert "git diff --name-only --no-renames" in _job("changes")


def test_a_code_file_renamed_to_markdown_is_a_code_change(tmp_path: Path) -> None:
    """Without --no-renames, git lists only the new path, which reads as docs only."""

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout

    git("init", "-q")
    (tmp_path / "x.py").write_text("print('a long enough file body for rename detection')\n" * 20)
    git("add", "x.py")
    git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD").strip()
    git("mv", "x.py", "x.md")
    git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "rename")
    default = git("diff", "--name-only", base, "HEAD")
    assert default == "x.md\n"
    assert decide(default.splitlines()).stdout == "code=false\n"
    fixed = git("diff", "--name-only", "--no-renames", base, "HEAD")
    assert sorted(fixed.splitlines()) == ["x.md", "x.py"]
    assert decide(fixed.splitlines()).stdout == "code=true\n"


GATE = require_path(ROOT / "scripts" / "ci-gate.sh")


def gate(changes: str, code: str, validation: str, test: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "CHANGES_RESULT": changes,
        "CODE": code,
        "VALIDATION_RESULT": validation,
        "TEST_RESULT": test,
    }
    return subprocess.run(["bash", str(GATE)], env=env, capture_output=True, text=True)


@pytest.mark.parametrize(
    ("changes", "code", "validation", "test", "exit_code"),
    [
        ("success", "true", "success", "success", 0),
        ("success", "false", "skipped", "skipped", 0),
        ("success", "true", "failure", "skipped", 1),
        ("success", "true", "success", "failure", 1),
        ("success", "true", "skipped", "skipped", 1),
        ("success", "true", "cancelled", "skipped", 1),
        ("success", "false", "success", "success", 1),
        ("success", "false", "skipped", "success", 1),
        ("failure", "", "skipped", "skipped", 1),
        ("failure", "true", "success", "success", 1),
        ("cancelled", "", "skipped", "skipped", 1),
        ("skipped", "", "skipped", "skipped", 1),
        ("success", "", "skipped", "skipped", 1),
        ("success", "maybe", "success", "success", 1),
    ],
)
def test_the_gate_passes_only_when_the_changes_job_and_the_code_jobs_agree(
    changes: str, code: str, validation: str, test: str, exit_code: int
) -> None:
    result = gate(changes, code, validation, test)
    assert result.returncode == exit_code, result.stderr
    assert result.stdout == ""
    assert (result.stderr != "") == (exit_code != 0)


def test_a_missing_result_exits_2_and_help_exits_0() -> None:
    missing = subprocess.run(
        ["bash", str(GATE)], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True
    )
    assert missing.returncode == 2 and "CHANGES_RESULT is not set" in missing.stderr
    helped = subprocess.run(["bash", str(GATE), "--help"], capture_output=True, text=True)
    assert helped.returncode == 0 and "Usage:" in helped.stdout


def test_the_result_job_runs_after_a_failure_and_reads_every_code_job() -> None:
    job = _job("ci-result")
    assert "needs: [changes, validation, test]" in job
    assert "if: always()" in job
    for variable in ("CHANGES_RESULT", "CODE", "VALIDATION_RESULT", "TEST_RESULT"):
        assert f"{variable}: ${{{{ needs." in job
    assert "run: bash scripts/ci-gate.sh" in job


_PATHS = st.one_of(
    st.sampled_from(["README.md", "docs/a.md", "docs/design/daily-prepare-tla.md", "src/x.py"]),
    st.builds(lambda stem, ext: stem + ext, _NAMES, st.sampled_from([".md", ".py", ".yml"])),
)


@settings(max_examples=60, deadline=None)
@given(paths=st.lists(_PATHS, max_size=8), order=st.randoms(use_true_random=False))
def test_the_answer_is_docs_only_when_every_path_is_markdown_and_none_is_tested(
    paths: list[str], order: Any
) -> None:
    shuffled = list(paths)
    order.shuffle(shuffled)
    docs_only = bool(paths) and all(
        p.endswith(".md") and p != "docs/design/daily-prepare-tla.md" for p in paths
    )
    expected = "code=false\n" if docs_only else "code=true\n"
    assert decide(paths).stdout == expected
    assert decide(shuffled).stdout == expected


def test_a_long_list_that_ends_early_does_not_break_the_writer_of_the_pipe() -> None:
    """The script reads the whole input, so `git diff` never gets SIGPIPE under pipefail."""
    script = f'set -o pipefail; seq 1 200000 | sed "s/^/src\\//" | bash "{SCRIPT}"'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert (result.returncode, result.stdout) == (0, "code=true\n")
