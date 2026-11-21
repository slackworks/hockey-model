#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# ///
"""Fail unless a release tag matches the package and the changelog (#99).

Usage: uv run python3 scripts/check_release.py --tag TAG [--dist DIR] [--notes-out PATH]
Other options: --pyproject PATH and --changelog PATH, for a test.

The checks, in order:

1. The tag is "v" followed by project.version, character for character.
2. project.version is a normal PEP 440 version, in its normal form.
It has no local part and no development release.
A pre-release such as 0.2.0rc1 is allowed, and the output marks it.
3. CHANGELOG.md has a release heading "## [VERSION]", with an optional " - YYYY-MM-DD" date.
The section holds text. "## [Unreleased]" does not count.
4. With --dist, DIR holds one wheel and one source archive, and both name this version.

--notes-out writes the text of the changelog section, for the release notes.

stdout: "version=X" and "prerelease=true|false", one for each line, ready for GITHUB_OUTPUT.
stderr: the failed check, with the fix.
Exit codes: 0 when every check passes, 1 when a check fails, 2 for a bad argument or a file
that is missing or unreadable.
"""

import argparse
import re
import sys
import tomllib
from datetime import date
from pathlib import Path

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parent.parent
# Keep a Changelog writes a release heading as "## [1.2.3] - 2026-01-31".
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ReleaseError(Exception):
    """A release check failed. ``setup`` is true when a file could not be read."""

    def __init__(self, message: str, *, setup: bool = False) -> None:
        super().__init__(message)
        self.setup = setup


def read_version(pyproject: Path) -> Version:
    """Return project.version as a normal PEP 440 version, or raise ``ReleaseError``."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"read {pyproject}: {exc}", setup=True) from exc
    text = data.get("project", {}).get("version")
    if not isinstance(text, str):
        raise ReleaseError(f"{pyproject} has no string project.version; set one, such as 0.1.0")
    try:
        version = Version(text)
    except InvalidVersion as exc:
        raise ReleaseError(
            f"project.version {text!r} is not a PEP 440 version; "
            "use a form such as 0.1.0 or 0.2.0rc1"
        ) from exc
    if str(version) != text:
        raise ReleaseError(
            f"project.version {text!r} is not in its normal form {str(version)!r}; "
            "the wheel name uses the normal form, so write that form"
        )
    if version.local is not None:
        raise ReleaseError(f"project.version {text!r} has a local part; a release has none")
    if version.is_devrelease:
        raise ReleaseError(
            f"project.version {text!r} is a development release; remove the .dev part to release"
        )
    return version


def check_tag(tag: str, version: Version) -> None:
    """Raise ``ReleaseError`` unless ``tag`` is exactly "v" followed by the version."""
    expected = f"v{version}"
    if tag != expected:
        raise ReleaseError(
            f"tag {tag!r} does not match project.version {str(version)!r}; "
            f"tag {expected!r}, or change project.version"
        )


def changelog_section(text: str, version: Version) -> str:
    """Return the body of the "## [VERSION]" section, or raise ``ReleaseError``."""
    heading = re.compile(rf"^## \[{re.escape(str(version))}\](?: - (?P<day>\S+))?[ \t]*$", re.M)
    found = heading.search(text)
    if found is None:
        raise ReleaseError(
            f"CHANGELOG.md has no heading '## [{version}]'; rename the Unreleased section "
            "to the release, and add the date"
        )
    day = found.group("day")
    if day is not None:
        _check_day(day, version)
    following = re.search(r"^## ", text[found.end() :], re.M)
    end = len(text) if following is None else found.end() + following.start()
    body = text[found.end() : end].strip()
    if not body:
        raise ReleaseError(f"CHANGELOG.md section '## [{version}]' is empty; list the changes")
    return body


def _check_day(day: str, version: Version) -> None:
    """Raise ``ReleaseError`` unless ``day`` is a real calendar date written YYYY-MM-DD."""
    if _DATE.fullmatch(day) is None:
        raise ReleaseError(f"CHANGELOG.md date {day!r} of [{version}] is not YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise ReleaseError(f"CHANGELOG.md date {day!r} of [{version}] is not a real date") from exc


def check_dist(dist: Path, version: Version) -> None:
    """Raise ``ReleaseError`` unless ``dist`` holds one wheel and one sdist for ``version``."""
    if not dist.is_dir():
        raise ReleaseError(f"{dist} is not a directory; build with `uv build --out-dir {dist}`")
    wheels = sorted(p.name for p in dist.glob("*.whl"))
    sdists = sorted(p.name for p in dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseError(
            f"{dist} must hold one wheel and one source archive; it holds {wheels + sdists}"
        )
    try:
        wheel_version = parse_wheel_filename(wheels[0])[1]
        sdist_version = parse_sdist_filename(sdists[0])[1]
    except (InvalidWheelFilename, InvalidSdistFilename) as exc:
        raise ReleaseError(f"{dist} holds a file with an invalid name: {exc}") from exc
    for name, found in ((wheels[0], wheel_version), (sdists[0], sdist_version)):
        if found != version:
            raise ReleaseError(
                f"{name} is version {found}, and project.version is {version}; "
                "build again from a clean checkout"
            )


def run(args: argparse.Namespace) -> tuple[Version, str]:
    """Run every check. Return the version and the changelog section."""
    version = read_version(args.pyproject)
    check_tag(args.tag, version)
    try:
        changelog = args.changelog.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"read {args.changelog}: {exc}", setup=True) from exc
    section = changelog_section(changelog, version)
    if args.dist is not None:
        check_dist(args.dist, version)
    return version, section


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="the tag that triggered the release")
    parser.add_argument("--dist", type=Path, help="a directory of built files to check")
    parser.add_argument("--notes-out", type=Path, help="write the changelog section here")
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md")
    args = parser.parse_args(argv)
    try:
        version, section = run(args)
        if args.notes_out is not None:
            args.notes_out.write_text(section + "\n", encoding="utf-8")
    except ReleaseError as exc:
        print(f"check_release: {exc}", file=sys.stderr)
        return 2 if exc.setup else 1
    except OSError as exc:
        print(f"check_release: write {args.notes_out}: {exc}", file=sys.stderr)
        return 2
    print(f"version={version}")
    print(f"prerelease={'true' if version.is_prerelease else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
