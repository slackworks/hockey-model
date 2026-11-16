"""Tests that no collected test module reads a repo-root file which a mutation copy leaves out.

A mutation run copies the package and the tests into ``mutants/``. That copy holds no
``.github`` directory, no ``specs`` directory, and no ``scripts`` directory. A test that
reads one of those files raises ``FileNotFoundError`` there, the stats phase aborts, and
every shard fails (#253). A guard is the fix, and this test keeps a new module from
forgetting it.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The repo-root directories that a mutation copy leaves out. ``fixtures`` and ``tests``
# stay out of this list: they live under tests/, which the copy carries.
MISSING_IN_COPY = ("scripts", "specs", ".github")
# A module reaches a guard by one of two shapes. The first names the copy. The second
# skips when the file it wants is absent, which is what the copy leaves.
_GUARDS = (
    re.compile(r'"mutants"\s+in\s+.*\.parts'),
    re.compile(r"if\s+not\s+[^\n]*\.exists\(\):\s*\n\s*(?:#[^\n]*\n\s*)?pytest\.skip\("),
)
_ROOT = re.compile(r"parents\[\d\]")


def _collected_modules() -> list[Path]:
    """Return each module that pytest collects as a test, which a mutation run runs."""
    return sorted((ROOT / "tests").rglob("test_*.py"))


def _has_guard(text: str) -> bool:
    """Return True when the module skips in the mutation copy by either guard shape."""
    return any(pattern.search(text) is not None for pattern in _GUARDS)


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
        "with no mutation-copy guard. A mutation copy holds no such directory, so add the skip: "
        'if "mutants" in Path(__file__).resolve().parts: pytest.skip(...)'
    )


def test_the_guard_regex_matches_the_guard_that_other_modules_use() -> None:
    # The detection above is only as good as these patterns. Pin each against a real guard.
    named = 'if "mutants" in Path(__file__).resolve().parts:\n    pytest.skip("no .github")'
    absent = 'if not SCRIPT.exists():\n    # The copy has no scripts.\n    pytest.skip("absent")\n'
    assert _has_guard(named) is True
    assert _has_guard(absent) is True
    assert _has_guard('"""A test of the mutants directory."""') is False
