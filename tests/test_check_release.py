"""Tests for the release gate in scripts/check_release.py (#99)."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "check_release.py")

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- A change that is not released.

## [0.1.0] - 2026-11-21

### Added

- The first release.

## [0.0.9] - 2026-10-01

- An older release.
"""


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_release = load()


def project(tmp_path: Path, version: str = "0.1.0", changelog: str = CHANGELOG) -> list[str]:
    """Write a pyproject and a changelog. Return the arguments that point the gate at them."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8")
    log = tmp_path / "CHANGELOG.md"
    log.write_text(changelog, encoding="utf-8")
    return ["--pyproject", str(pyproject), "--changelog", str(log)]


def gate(tmp_path: Path, tag: str, *extra: str, **files: str) -> int:
    return check_release.main(["--tag", tag, *project(tmp_path, **files), *extra])


def test_a_matching_tag_and_changelog_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate(tmp_path, "v0.1.0") == 0
    assert capsys.readouterr().out == "version=0.1.0\nprerelease=false\n"


@pytest.mark.parametrize(
    "tag", ["0.1.0", "v0.1.1", "V0.1.0", "v0.1.0 ", " v0.1.0", "v0.1", "vv0.1.0"]
)
def test_a_tag_that_is_not_v_and_the_version_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tag: str
) -> None:
    assert gate(tmp_path, tag) == 1
    assert "does not match project.version '0.1.0'" in capsys.readouterr().err


@pytest.mark.parametrize("version", ["0.1", "1.0.0.0.0", "1.2.3"])
def test_any_normal_version_passes_with_its_own_tag(tmp_path: Path, version: str) -> None:
    log = f"## [{version}] - 2026-01-01\n\n- A change.\n"
    assert gate(tmp_path, f"v{version}", version=version, changelog=log) == 0


@pytest.mark.parametrize(
    ("version", "problem"),
    [
        ("not-a-version", "is not a PEP 440 version"),
        ("01.2", "is not in its normal form '1.2'"),
        ("1.0.0+local", "has a local part"),
        ("1.0.0.dev1", "is a development release"),
        ("1.0.0-rc.1", "is not in its normal form '1.0.0rc1'"),
    ],
)
def test_a_version_that_is_not_publishable_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str, problem: str
) -> None:
    log = f"## [{version}]\n\n- A change.\n"
    assert gate(tmp_path, f"v{version}", version=version, changelog=log) == 1
    assert problem in capsys.readouterr().err


def test_a_prerelease_passes_and_is_marked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = "## [0.2.0rc1] - 2026-12-01\n\n- A candidate.\n"
    assert gate(tmp_path, "v0.2.0rc1", version="0.2.0rc1", changelog=log) == 0
    assert capsys.readouterr().out == "version=0.2.0rc1\nprerelease=true\n"


def test_a_post_release_is_not_a_prerelease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = "## [0.2.0.post1] - 2026-12-01\n\n- A fix.\n"
    assert gate(tmp_path, "v0.2.0.post1", version="0.2.0.post1", changelog=log) == 0
    assert "prerelease=false" in capsys.readouterr().out


@pytest.mark.parametrize(
    "log",
    [
        "## [Unreleased]\n\n- Everything.\n",
        "## [0.1.0rc1] - 2026-11-21\n\n- A candidate.\n",
        "## [10.1.0] - 2026-11-21\n\n- Other.\n",
        "## [0.1.0.1] - 2026-11-21\n\n- Other.\n",
        "### [0.1.0]\n\n- Wrong level.\n",
        "Release 0.1.0 is described in [0.1.0] below.\n",
        "## 0.1.0\n\n- No brackets.\n",
        "- ## [0.1.0] inside a list\n",
        "",
    ],
)
def test_a_changelog_with_no_exact_heading_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], log: str
) -> None:
    assert gate(tmp_path, "v0.1.0", changelog=log) == 1
    assert "has no heading '## [0.1.0]'" in capsys.readouterr().err


def test_an_empty_section_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = "## [0.1.0] - 2026-11-21\n\n## [0.0.9] - 2026-10-01\n\n- Older.\n"
    assert gate(tmp_path, "v0.1.0", changelog=log) == 1
    assert "is empty" in capsys.readouterr().err


@pytest.mark.parametrize("day", ["2026-13-01", "2026-02-30", "21-11-2026", "2026-1-1", "today"])
def test_a_bad_date_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str], day: str) -> None:
    log = f"## [0.1.0] - {day}\n\n- A change.\n"
    assert gate(tmp_path, "v0.1.0", changelog=log) == 1
    assert "date" in capsys.readouterr().err


@pytest.mark.parametrize("heading", ["## [0.1.0]", "## [0.1.0] - 2026-11-21", "## [0.1.0]  "])
def test_a_heading_with_or_without_a_date_passes(tmp_path: Path, heading: str) -> None:
    assert gate(tmp_path, "v0.1.0", changelog=f"{heading}\n\n- A change.\n") == 0


def test_the_notes_hold_the_section_and_stop_at_the_next_release(tmp_path: Path) -> None:
    out = tmp_path / "notes.md"
    assert gate(tmp_path, "v0.1.0", "--notes-out", str(out)) == 0
    assert out.read_text(encoding="utf-8") == "### Added\n\n- The first release.\n"


def test_the_last_section_runs_to_the_end_of_the_file(tmp_path: Path) -> None:
    out = tmp_path / "notes.md"
    log = "## [0.1.0] - 2026-11-21\n\n- Only one.\n"
    assert gate(tmp_path, "v0.1.0", "--notes-out", str(out), changelog=log) == 0
    assert out.read_text(encoding="utf-8") == "- Only one.\n"


def test_a_missing_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--tag", "v0.1.0", "--pyproject", str(tmp_path / "none.toml")]
    assert check_release.main(args) == 2
    assert "none.toml" in capsys.readouterr().err


def test_a_pyproject_with_no_version_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert check_release.main(["--tag", "v0.1.0", "--pyproject", str(pyproject)]) == 1
    assert "no string project.version" in capsys.readouterr().err


def dist(tmp_path: Path, *names: str) -> Path:
    folder = tmp_path / "dist"
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(b"x")
    return folder


WHEEL = "hockey_model-0.1.0-py3-none-any.whl"
SDIST = "hockey_model-0.1.0.tar.gz"


def test_a_dist_of_one_wheel_and_one_sdist_of_the_version_passes(tmp_path: Path) -> None:
    folder = dist(tmp_path, WHEEL, SDIST)
    assert gate(tmp_path, "v0.1.0", "--dist", str(folder)) == 0


@pytest.mark.parametrize(
    ("names", "problem"),
    [
        ((WHEEL,), "must hold one wheel and one source archive"),
        ((WHEEL, SDIST, "hockey_model-0.1.0-py3-none-any.whl.bak.whl"), "one wheel"),
        (("hockey_model-0.1.1-py3-none-any.whl", SDIST), "is version 0.1.1"),
        ((WHEEL, "hockey_model-0.0.9.tar.gz"), "is version 0.0.9"),
        (("bad.whl", SDIST), "invalid name"),
        ((), "must hold one wheel"),
    ],
)
def test_a_dist_that_does_not_fit_the_version_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], names: tuple[str, ...], problem: str
) -> None:
    folder = dist(tmp_path, *names)
    assert gate(tmp_path, "v0.1.0", "--dist", str(folder)) == 1
    assert problem in capsys.readouterr().err


def test_a_missing_dist_folder_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert gate(tmp_path, "v0.1.0", "--dist", str(tmp_path / "none")) == 1
    assert "is not a directory" in capsys.readouterr().err


_PARTS = st.lists(st.integers(min_value=0, max_value=99), min_size=1, max_size=4)


@given(parts=_PARTS)
def test_the_tag_v_plus_the_version_passes_and_any_other_tag_fails(parts: list[int]) -> None:
    version = check_release.Version(".".join(str(p) for p in parts))
    check_release.check_tag(f"v{version}", version)
    for other in (str(version), f"V{version}", f"v{version}.1", f"v{version} ", f"v {version}"):
        with pytest.raises(check_release.ReleaseError):
            check_release.check_tag(other, version)
