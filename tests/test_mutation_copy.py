"""Tests for the one guard a script test uses when it needs a path the mutation copy leaves out.

A mutation run copies ``src/`` and ``tests/`` into ``mutants/``. That copy holds no
``scripts``, ``specs``, or ``.github`` directory. A test that reads one of those paths
raises ``FileNotFoundError`` there, and the run aborts (#253). The guard skips in the copy.
In a normal checkout the same missing path is a broken test, and the guard fails (#198).
"""

from pathlib import Path

import pytest
from mutation_copy import in_mutation_copy, require_path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_a_copy_path_is_a_mutation_copy() -> None:
    assert in_mutation_copy(Path("/work/repo/mutants/tests/test_x.py"))
    assert in_mutation_copy(Path("/work/repo/mutants/src/hockey_model/x.py"))


def test_a_checkout_path_is_not_a_mutation_copy() -> None:
    assert not in_mutation_copy(Path("/work/repo/tests/test_x.py"))
    assert not in_mutation_copy(Path("/work/repo/src/hockey_model/mutants.py"))
    assert not in_mutation_copy(Path("/work/repo/tests/test_mutants.py"))


@pytest.mark.parametrize("missing", ["no_such_script.py", "gone.sh"])
def test_a_missing_path_fails_in_a_checkout(missing: str) -> None:
    with pytest.raises(FileNotFoundError, match="mutation copy"):
        require_path(SCRIPTS / missing)


def test_a_missing_directory_fails_in_a_checkout() -> None:
    with pytest.raises(FileNotFoundError, match="mutation copy"):
        require_path(SCRIPTS / "no_such_dir")


def test_a_missing_path_skips_in_a_mutation_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mutation_copy.in_mutation_copy", lambda _path=None: True)
    with pytest.raises(pytest.skip.Exception, match="copies src and tests only"):
        require_path(SCRIPTS / "no_such_script.py")


def test_a_present_script_is_returned() -> None:
    script = require_path(SCRIPTS / "valid-day.sh")
    assert script == SCRIPTS / "valid-day.sh"
    assert script.exists()


def test_a_present_directory_is_returned() -> None:
    assert require_path(SCRIPTS) == SCRIPTS
