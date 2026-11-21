"""Tests for the release gate in scripts/check_release.py (#99)."""

import importlib.util
import json
import zipfile
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


def write_wheel(path: Path, metadata_version: str | None) -> None:
    """Write a wheel file. Its METADATA names ``metadata_version``, and None omits the file."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hockey_model/__init__.py", "")
        if metadata_version is not None:
            text = f"Metadata-Version: 2.4\nName: hockey-model\nVersion: {metadata_version}\n"
            archive.writestr("hockey_model-x.dist-info/METADATA", text)


def dist(tmp_path: Path, *names: str, metadata: str | None = "name") -> Path:
    """Write a dist folder. A wheel holds METADATA of the version in its name unless told."""
    folder = tmp_path / "dist"
    folder.mkdir()
    for name in names:
        if name.endswith(".whl"):
            parts = name.split("-")
            inside = parts[1] if metadata == "name" and len(parts) > 1 else metadata
            write_wheel(folder / name, inside)
        else:
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


@pytest.mark.parametrize(
    ("metadata", "problem"),
    [
        ("0.1.1", "METADATA is version 0.1.1"),
        (None, "holds 0 METADATA files"),
        ("not a version", "no valid Version in its METADATA"),
    ],
)
def test_a_wheel_whose_metadata_names_another_version_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], metadata: str | None, problem: str
) -> None:
    folder = dist(tmp_path, WHEEL, SDIST, metadata=metadata)
    assert gate(tmp_path, "v0.1.0", "--dist", str(folder)) == 1
    assert problem in capsys.readouterr().err


def test_a_wheel_that_is_not_a_zip_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = dist(tmp_path, SDIST)
    (folder / WHEEL).write_bytes(b"not a zip")
    assert gate(tmp_path, "v0.1.0", "--dist", str(folder)) == 1
    assert "not a zip file" in capsys.readouterr().err


def test_a_pyproject_that_is_not_utf8_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes(b'[project]\nversion = "\xff"\n')
    assert check_release.main(["--tag", "v0.1.0", "--pyproject", str(pyproject)]) == 2
    assert "pyproject.toml" in capsys.readouterr().err


def test_a_changelog_that_is_not_utf8_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = project(tmp_path)
    (tmp_path / "CHANGELOG.md").write_bytes(b"## [0.1.0]\n\xff\n")
    assert check_release.main(["--tag", "v0.1.0", *args]) == 2
    assert "CHANGELOG.md" in capsys.readouterr().err


def test_a_project_that_is_not_a_table_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('project = "x"\n', encoding="utf-8")
    assert check_release.main(["--tag", "v0.1.0", "--pyproject", str(pyproject)]) == 1
    assert "no string project.version" in capsys.readouterr().err


def test_a_notes_file_that_cannot_be_written_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "none" / "notes.md"
    assert gate(tmp_path, "v0.1.0", "--notes-out", str(out)) == 2
    assert "write" in capsys.readouterr().err


def test_a_dist_error_that_is_not_a_write_does_not_name_a_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate(tmp_path, "v0.1.0", "--dist", str(tmp_path / "none")) == 1
    assert "write" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    [
        "### Added\n\n### Fixed\n",
        "\n\n",
        "[Unreleased]: https://example.test/compare\n",
        "### Added\n\n[Unreleased]: https://example.test/compare\n",
    ],
)
def test_a_section_of_only_headings_and_links_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    log = f"## [0.1.0] - 2026-11-21\n\n{body}"
    assert gate(tmp_path, "v0.1.0", changelog=log) == 1
    assert "is empty" in capsys.readouterr().err


def test_link_definitions_at_the_end_are_not_in_the_notes(tmp_path: Path) -> None:
    out = tmp_path / "notes.md"
    log = "## [0.1.0] - 2026-11-21\n\n- A change.\n\n[0.1.0]: https://example.test/tag\n"
    assert gate(tmp_path, "v0.1.0", "--notes-out", str(out), changelog=log) == 0
    assert out.read_text(encoding="utf-8") == "- A change.\n"


_TEXT = st.text(max_size=40)


@given(text=_TEXT)
def test_read_version_accepts_only_a_normal_version_or_raises_a_release_error(
    tmp_path_factory: pytest.TempPathFactory, text: str
) -> None:
    pyproject = tmp_path_factory.mktemp("rv") / "pyproject.toml"
    pyproject.write_text(f"[project]\nversion = {json.dumps(text)}\n", encoding="utf-8")
    try:
        version = check_release.read_version(pyproject)
    except check_release.ReleaseError:
        return
    assert str(version) == text
    assert version.local is None and not version.is_devrelease


@given(parts=_PARTS)
def test_read_version_accepts_every_normal_release_version(
    tmp_path_factory: pytest.TempPathFactory, parts: list[int]
) -> None:
    text = ".".join(str(p) for p in parts)
    pyproject = tmp_path_factory.mktemp("rv") / "pyproject.toml"
    pyproject.write_text(f'[project]\nversion = "{text}"\n', encoding="utf-8")
    assert str(check_release.read_version(pyproject)) == text


_LINES = st.text(alphabet="abc -.#[]:/", max_size=12).filter(lambda t: not t.startswith("## "))


@given(before=st.lists(_LINES, max_size=4), body=st.lists(_LINES, min_size=1, max_size=4))
def test_the_section_of_a_heading_is_its_body_up_to_the_next_release(
    before: list[str], body: list[str]
) -> None:
    version = check_release.Version("1.2.3")
    text = "\n".join([*before, "## [1.2.3]", *body, "## [1.2.2]", "- old"])
    content = [line for line in body if line.strip() and not line.startswith("#")]
    links = [line for line in body if check_release._LINK_DEFINITION.fullmatch(line)]
    if not content or content == links:
        with pytest.raises(check_release.ReleaseError):
            check_release.changelog_section(text, version)
        return
    section = check_release.changelog_section(text, version)
    assert "- old" not in section and "## [1.2.3]" not in section


@given(text=_TEXT)
def test_the_changelog_search_raises_only_a_release_error(text: str) -> None:
    try:
        check_release.changelog_section(text, check_release.Version("1.2.3"))
    except check_release.ReleaseError:
        pass


@given(good=_PARTS, other=_PARTS)
def test_a_dist_passes_for_its_own_version_and_fails_for_any_other(
    tmp_path_factory: pytest.TempPathFactory, good: list[int], other: list[int]
) -> None:
    text = ".".join(str(p) for p in good)
    folder = tmp_path_factory.mktemp("d")
    write_wheel(folder / f"hockey_model-{text}-py3-none-any.whl", text)
    (folder / f"hockey_model-{text}.tar.gz").write_bytes(b"x")
    check_release.check_dist(folder, check_release.Version(text))
    different = check_release.Version(".".join(str(p) for p in other))
    if different != check_release.Version(text):
        with pytest.raises(check_release.ReleaseError, match="is version"):
            check_release.check_dist(folder, different)
