"""Tests that no test module reads a repo-root file which a mutation copy leaves out.

A mutation run copies ``src/``, ``tests/``, ``pyproject.toml``, and ``uv.lock`` into
``mutants/``. Every other repo-root entry is absent there. A test that reads one of them
raises ``FileNotFoundError``, the stats phase aborts, and every shard fails (#253, #323).
The guard is ``require_path`` in ``tests/mutation_copy.py``, and this test keeps a new
module from forgetting it.
"""

import ast
import re
import subprocess
from pathlib import Path

import pytest
from mutation_copy import in_mutation_copy

ROOT = Path(__file__).resolve().parents[1]
# What the mutation copy carries, as repo-root names.
COPIED = ("src", "tests", "pyproject.toml", "uv.lock")
# The repo-root entries that a mutation copy leaves out. ``fixtures`` and ``tests`` stay out of
# this list: they live under tests/, which the copy carries. A test below keeps the list equal
# to the tracked files of the repository.
MISSING_IN_COPY = (
    "scripts",
    "specs",
    ".github",
    "docs",
    "experiments",
    "README.md",
    "CHANGELOG.md",
    "AGENTS.md",
    "renovate.json",
    ".pre-commit-config.yaml",
    ".editorconfig",
    ".editorconfig-checker.json",
    ".python-version",
    ".gitignore",
    ".llmenv-allow-main",
    ".llmenv.yaml",
)
# A path built from the module's own file or from the working directory. A test runs with the
# copy as its working directory, so both reach the copy and not the repo root.
_ROOT_WALK = re.compile(r"__file__|Path\.cwd\(\)|getcwd\(\)")
# The repo-root scripts path that tests/report_script_support.py exports. A module imports it
# and names no quoted directory, so the walk check cannot see the read (#323).
_SCRIPTS_CONSTANT = re.compile(r"\bSCRIPTS\b")
# A test module that calls one of these never names a repo-root path itself, because the
# helper resolves it. The helper guards the read (#155). Listing the names here makes that
# promise checkable: a new helper that reads a repo-root path must guard it or join this set.
_GUARDED_HELPERS = ("report_script_support",)
# These two hold the guard and its check, so they name every directory on purpose.
_EXEMPT = ("mutation_copy.py", "test_mutation_copy_reads.py")


def _checked_modules() -> list[Path]:
    """Return each Python file under tests/ that a mutation run imports or collects."""
    return sorted(p for p in (ROOT / "tests").rglob("*.py") if p.name not in _EXEMPT)


def _call_name(node: ast.Call) -> str:
    """Return the plain name that a call uses, or an empty string for a computed callee."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _has_guard(text: str) -> bool:
    """Return True when the module calls the mutation-copy guard.

    A comment, a string, an import, and a ``def`` of the same name are not calls.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return any(isinstance(n, ast.Call) and _call_name(n) == "require_path" for n in ast.walk(tree))


def _names_entry(text: str, name: str) -> bool:
    """Return True when a string in ``text`` starts with the repo-root entry ``name``.

    ``"scripts"`` and ``"scripts/tlc.sh"`` both match. ``"scripts_dir"`` does not.
    """
    return re.search(rf"[\"']{re.escape(name)}(?:/|[\"'])", text) is not None


def _unguarded_reads(path: Path) -> list[str]:
    """Return each missing-in-copy entry that ``path`` names without a guard.

    A module reaches a repo-root path by walking up from its own file, or through the
    ``SCRIPTS`` constant. The guard call clears the whole module.
    """
    text = path.read_text(encoding="utf-8")
    if _has_guard(text):
        return []
    walks = _ROOT_WALK.search(text) is not None
    named = {name for name in MISSING_IN_COPY if walks and _names_entry(text, name)}
    if _SCRIPTS_CONSTANT.search(text):
        named.add("scripts")
    return sorted(named)


@pytest.mark.parametrize("path", _checked_modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_a_repo_root_read_survives_the_mutation_copy(path: Path) -> None:
    unguarded = _unguarded_reads(path)
    assert unguarded == [], (
        f"{path.relative_to(ROOT)} names {', '.join(unguarded)} through a walk up from its own "
        "path or the SCRIPTS constant, with no mutation-copy guard. A mutation copy holds no "
        "such entry, so call require_path from tests/mutation_copy.py on the path before its "
        "first read."
    )


def _module(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "test_probe.py"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("from report_script_support import SCRIPTS\nX = SCRIPTS / 'a.sh'\n", ["scripts"]),
        (
            "from report_script_support import SCRIPTS\nrequire_path(SCRIPTS / 'a.sh')\n",
            [],
        ),
        ("from p import SCRIPTS\n# require_path(SCRIPTS)\nX = SCRIPTS\n", ["scripts"]),
        ("from p import SCRIPTS\nX = 'require_path(SCRIPTS)'\nY = SCRIPTS\n", ["scripts"]),
        ("from p import SCRIPTS\n\ndef require_path(p):\n    return SCRIPTS\n", ["scripts"]),
        ('P = Path(__file__).resolve().parents[1] / "specs" / "a.tla"\n', ["specs"]),
        ('P = Path(__file__).parent.parent / "scripts" / "a.sh"\n', ["scripts"]),
        ('P = Path(__file__).parents[1] / "scripts/tlc.sh"\n', ["scripts"]),
        ('P = Path.cwd() / "scripts" / "a.sh"\n', ["scripts"]),
        ("P = Path(__file__).parents[1] / 'docs' / 'a.md'\n", ["docs"]),
        ('P = Path(__file__).parents[1] / "README.md"\n', ["README.md"]),
        ('P = Path(__file__).parent / "fixtures" / "a.json"\n', []),
        ('P = Path(__file__).parents[1] / "scripts_dir"\n', []),
        ('P = "specs"\n', []),
        ('P = Path(__file__).parents[1] / "specs"\nrequire_path(P)\n', []),
        ("this is not python (\nSCRIPTS\n", ["scripts"]),
    ],
    ids=[
        "scripts-constant",
        "scripts-constant-guarded",
        "guard-in-comment",
        "guard-in-string",
        "guard-def-is-not-a-call",
        "specs-walk",
        "parent-parent-walk",
        "slash-joined-name",
        "cwd-walk",
        "single-quoted-docs",
        "root-file",
        "fixtures-are-copied",
        "longer-name",
        "name-with-no-walk",
        "walk-guarded",
        "unparsable-module-is-unguarded",
    ],
)
def test_the_check_names_what_a_module_reads_without_a_guard(
    tmp_path: Path, body: str, expected: list[str]
) -> None:
    assert _unguarded_reads(_module(tmp_path, body)) == expected


def test_the_guard_detector_matches_the_guard_that_other_modules_use() -> None:
    called = 'SCRIPT = require_path(Path(__file__).resolve().parents[1] / "scripts" / "tlc.sh")'
    imported = "from mutation_copy import require_path\n"
    through_module = "import mutation_copy\nmutation_copy.require_path(P)\n"
    assert _has_guard(called) is True
    assert _has_guard(through_module) is True
    assert _has_guard(imported) is False
    assert _has_guard('"""A test of the mutants directory."""') is False


def _tracked_root_entries() -> set[str]:
    """Return the first path segment of every file that git tracks."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return {line.split("/", 1)[0] for line in listing.split("\0") if line}


def test_the_missing_in_copy_list_names_every_tracked_root_entry() -> None:
    """A new top-level entry joins the list, or this test fails (#323)."""
    if in_mutation_copy() or not (ROOT / ".git").exists():
        pytest.skip("only a checkout has git history to list")
    leftover = _tracked_root_entries() - set(COPIED) - set(MISSING_IN_COPY)
    assert not leftover, (
        f"{sorted(leftover)} are tracked at the repo root, and a mutation copy leaves them out. "
        "Add each one to MISSING_IN_COPY, or to COPIED if mutmut copies it."
    )
    assert not set(MISSING_IN_COPY) - _tracked_root_entries(), (
        "MISSING_IN_COPY names an entry that git does not track. Remove the stale name."
    )


@pytest.mark.parametrize("name", _GUARDED_HELPERS)
def test_a_guarded_helper_resolves_every_repo_root_path_it_names(name: str) -> None:
    """A helper that a test module trusts must guard each missing-in-copy path it resolves.

    The per-module check above cannot see this: a test module that calls ``_run`` names a bare
    file name, so the module has no ``"scripts"`` string for the check to find, and the read
    happens in the helper (#155). The helper must guard the path itself.
    """
    text = (ROOT / "tests" / f"{name}.py").read_text(encoding="utf-8")
    for directory in MISSING_IN_COPY:
        if not _names_entry(text, directory):
            continue
        assert _has_guard(text), (
            f"tests/{name}.py resolves {directory}/ and calls no require_path. A mutation copy "
            "holds no such directory, so guard the read where it happens."
        )
