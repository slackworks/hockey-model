"""The one guard a test uses for a repo-root path that a mutation copy leaves out.

A mutation run copies ``src/`` and ``tests/`` into ``mutants/``. That copy holds no
``scripts``, ``specs``, or ``.github`` directory, so a test that reads one of those paths
raises ``FileNotFoundError`` and the run aborts (#253). The guard skips there.

In a normal checkout the copy is absent, so a missing path is a broken test. Skipping then
would hide the break: deleting ``scripts/tlc.sh`` would delete its tests with it (#198).
"""

from pathlib import Path

import pytest

# The directory that mutmut writes its copy into. It sits at the repo root, so it is one
# part of the path of every file a mutation run collects.
COPY_DIR = "mutants"


def in_mutation_copy(path: Path | None = None) -> bool:
    """Return True when ``path`` sits inside a mutation copy, or the default when it is None.

    The copy is a ``mutants`` directory whose parent holds ``pyproject.toml``, which is the repo
    root. A checkout that lives under a folder named ``mutants`` has no such marker above it.
    """
    target = (path if path is not None else Path(__file__)).resolve()
    return any(
        parent.name == COPY_DIR and (parent.parent / "pyproject.toml").is_file()
        for parent in target.parents
    )


def require_path(path: Path) -> Path:
    """Return ``path`` after proving it exists. Skip in a mutation copy; fail otherwise.

    ``path`` is a file or a directory. The caller runs this at module level, so a failure
    stops collection and the test module never reports a pass for a path that is gone.
    """
    if in_mutation_copy():
        pytest.skip(
            f"the mutation copy copies src and tests only, so {path} is absent",
            allow_module_level=True,
        )
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing from a normal checkout, where only a mutation copy may "
            f"leave it out. Restore it, or delete the tests that read it."
        )
    return path
