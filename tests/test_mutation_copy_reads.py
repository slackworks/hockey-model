"""Tests that no collected test module reads a repo-root file which a mutation copy leaves out.

A mutation run copies the package and the tests into ``mutants/``. That copy holds no
``.github`` directory, no ``specs`` directory, and no ``scripts`` directory. A test that
reads one of those files raises ``FileNotFoundError`` there, the stats phase aborts, and
every shard fails (#253). The guard is ``require_path`` in ``tests/mutation_copy.py``, and
this test keeps a new module from forgetting it.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The repo-root directories that a mutation copy leaves out. ``fixtures`` and ``tests``
# stay out of this list: they live under tests/, which the copy carries.
MISSING_IN_COPY = ("scripts", "specs", ".github")
# A module reaches the guard by calling the helper, directly or through the import.
_GUARD = re.compile(r"require_path\(")
_ROOT = re.compile(r"parents\[\d\]")
# A test module that calls one of these never names a repo-root path itself, because the
# helper resolves it. The helper guards the read (#155). Listing the names here makes that
# promise checkable: a new helper that reads a repo-root path must guard it or join this set.
_GUARDED_HELPERS = ("report_script_support",)


def _collected_modules() -> list[Path]:
    """Return each module that pytest collects as a test, which a mutation run runs."""
    return sorted((ROOT / "tests").rglob("test_*.py"))


def _has_guard(text: str) -> bool:
    """Return True when the module calls the mutation-copy guard."""
    return _GUARD.search(text) is not None


def _unguarded_reads(path: Path) -> list[str]:
    """Return each missing-in-copy directory that ``path`` names without a guard."""
    text = path.read_text(encoding="utf-8")
    if _has_guard(text) or not _ROOT.search(text):
        return []
    return [name for name in MISSING_IN_COPY if f'"{name}"' in text]


@pytest.mark.parametrize("path", _collected_modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_a_repo_root_read_survives_the_mutation_copy(path: Path) -> None:
    unguarded = _unguarded_reads(path)
    assert unguarded == [], (
        f"{path.relative_to(ROOT)} names {', '.join(unguarded)} and walks up from its own path, "
        "with no mutation-copy guard. A mutation copy holds no such directory, so call "
        "require_path from tests/mutation_copy.py on the path before its first read."
    )


def test_the_guard_regex_matches_the_guard_that_other_modules_use() -> None:
    # The detection above is only as good as this pattern. Pin it against the real call,
    # and against the import alone, which is not a guard until it is called.
    called = 'SCRIPT = require_path(Path(__file__).resolve().parents[1] / "scripts" / "tlc.sh")'
    imported = "from mutation_copy import require_path\n"
    assert _has_guard(called) is True
    assert _has_guard(imported) is False
    assert _has_guard('"""A test of the mutants directory."""') is False


@pytest.mark.parametrize("name", _GUARDED_HELPERS)
def test_a_guarded_helper_resolves_every_repo_root_path_it_names(name: str) -> None:
    """A helper that a test module trusts must guard each missing-in-copy path it resolves.

    The per-module check above cannot see this: a test module that calls ``_run`` names a bare
    file name, so the module has no ``"scripts"`` string for the check to find, and the read
    happens in the helper (#155). The helper must guard the path itself.
    """
    text = (ROOT / "tests" / f"{name}.py").read_text(encoding="utf-8")
    for directory in MISSING_IN_COPY:
        if f'"{directory}"' not in text:
            continue
        assert _has_guard(text), (
            f"tests/{name}.py resolves {directory}/ and calls no require_path. A mutation copy "
            "holds no such directory, so guard the read where it happens."
        )
