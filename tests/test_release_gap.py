"""The release check of scripts/release-gap.sh (#341)."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from report_script_support import DAY, SCRIPTS, TAG, _env

NEWEST_AT = "2026-11-04T10:00:00.000000+00:00"
_BUILD_TIMES = ["000000", "090000", "100000", "235959"]
_ASSET_POOL = [
    *(f"report-{DAY}-{t}Z.{ext}" for t in _BUILD_TIMES for ext in ("json", "md")),
    f"index-{DAY}.json",
    f"report-{DAY}.json",
    f"report-{DAY}-100000Z.txt",
]


def _gap(fakes: Path, tmp_path: Path, newest_file: str, assets: list[str]) -> str:
    """Return the gap reason of release_gap over a fake release that names ``newest_file``."""
    result = _gap_run(fakes, tmp_path, newest_file, assets)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _gap_run(
    fakes: Path, tmp_path: Path, newest_file: str, assets: list[str], notes: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run release_gap over a fake release whose index names ``newest_file``."""
    remote = tmp_path / "remote"
    remote.mkdir()
    index = {"builds": [{"file": newest_file, "generated_at": NEWEST_AT}]}
    (remote / f"index-{DAY}.json").write_text(json.dumps(index), encoding="utf-8")
    (remote / f"report-{DAY}.json").write_text(
        json.dumps({"generated_at": NEWEST_AT}), encoding="utf-8"
    )
    work = tmp_path / "work"
    work.mkdir()
    env = _env(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS="\n".join(assets),
        FAKE_NOTES=notes,
        FAKE_REMOTE_DIR=str(remote),
    )
    script = f'source "{SCRIPTS}/release-gap.sh"; release_gap "{TAG}" "{work}" "{DAY}"'
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    return result


def test_a_release_without_the_version_md_of_its_newest_build_is_behind(
    fakes: Path, tmp_path: Path
) -> None:
    build = f"report-{DAY}-100000Z"
    assets = [f"index-{DAY}.json", f"report-{DAY}.json", f"{build}.json"]
    answer = _gap(fakes, tmp_path, f"{build}.json", assets)
    assert f"it has no {build}.md" in answer


def test_a_release_with_every_version_file_of_its_newest_build_has_no_gap_in_files(
    fakes: Path, tmp_path: Path
) -> None:
    build = f"report-{DAY}-100000Z"
    assets = [f"index-{DAY}.json", f"report-{DAY}.json", f"{build}.json", f"{build}.md"]
    answer = _gap(fakes, tmp_path, f"{build}.json", assets)
    assert "version file" not in answer


def test_a_build_name_that_starts_with_a_dash_is_not_read_as_a_grep_flag(
    fakes: Path, tmp_path: Path
) -> None:
    # An unguarded grep reads -ereport-DAY.json as the pattern report-DAY.json, which the alias
    # asset matches, so the check would pass for a build that has no version files.
    answer = _gap(
        fakes,
        tmp_path,
        f"-ereport-{DAY}.json",
        [f"index-{DAY}.json", f"report-{DAY}.json"],
    )
    assert f"it has no -ereport-{DAY}.json" in answer


def test_an_orphan_version_file_is_a_warning_and_not_a_gap(fakes: Path, tmp_path: Path) -> None:
    """A publish that fails after it uploads build B's JSON leaves an orphan the index never
    lists, so a gap would republish every run. The check warns and reports no gap (#349)."""
    build = f"report-{DAY}-100000Z"
    orphan = f"report-{DAY}-090000Z.json"
    assets = [f"index-{DAY}.json", f"report-{DAY}.json", f"{build}.json", f"{build}.md", orphan]
    notes = f"Built at 10:00 UTC, {build}."
    result = _gap_run(fakes, tmp_path, f"{build}.json", assets, notes=notes)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert f"::warning::daily-report: release {TAG} holds {orphan}" in result.stderr


def test_a_listed_version_file_is_not_an_orphan(fakes: Path, tmp_path: Path) -> None:
    build = f"report-{DAY}-100000Z"
    assets = [f"index-{DAY}.json", f"report-{DAY}.json", f"{build}.json", f"{build}.md"]
    result = _gap_run(fakes, tmp_path, f"{build}.json", assets)
    assert "::warning::" not in result.stderr


def test_a_grep_error_is_a_failure_and_not_no_orphan(tmp_path: Path) -> None:
    """A day that is not a valid pattern makes grep exit 2. That must not read as no orphan
    (#349), so the matcher returns 1 with a message."""
    index = tmp_path / "index.json"
    index.write_text('{"builds": []}', encoding="utf-8")
    script = f'source "{SCRIPTS}/release-gap.sh"; unlisted_version_files "x" "{index}" "("'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "grep exit 2" in result.stderr


@settings(max_examples=40, deadline=None)
@given(
    names=st.lists(st.sampled_from(_ASSET_POOL), unique=True, max_size=8),
    listed=st.lists(st.sampled_from(_BUILD_TIMES), unique=True, max_size=4),
)
def test_the_orphan_matcher_names_exactly_the_unlisted_version_files(
    names: list[str], listed: list[str]
) -> None:
    """For any asset names and any index, the matcher prints exactly the version files whose
    build the index does not list, in the order of the asset names (#349)."""
    with tempfile.TemporaryDirectory() as folder:
        index = Path(folder) / "index.json"
        builds = [{"file": f"report-{DAY}-{t}Z.json", "generated_at": NEWEST_AT} for t in listed]
        index.write_text(json.dumps({"builds": builds}), encoding="utf-8")
        script = (
            f'source "{SCRIPTS}/release-gap.sh"; unlisted_version_files "$NAMES" "{index}" "{DAY}"'
        )
        env = {"PATH": os.environ["PATH"], "NAMES": "\n".join(names)}
        result = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
        )
    expected = [
        name
        for name in names
        if re.fullmatch(rf"report-{DAY}-[0-9]{{6}}Z\.(json|md)", name)
        and name.rsplit(".", 1)[0] not in {f"report-{DAY}-{t}Z" for t in listed}
    ]
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected


def test_a_failed_release_call_returns_1_inside_an_if_guard(fakes: Path, tmp_path: Path) -> None:
    # The callers run release_gap inside an if-guard, where set -e is off (#340). The function
    # must return 1 on its own, or the caller would read the failure as no gap.
    work = tmp_path / "work"
    work.mkdir()
    env = _env(fakes, tmp_path, FAKE_GH_FAIL="1")
    script = (
        f'source "{SCRIPTS}/release-gap.sh"\n'
        f'if release_gap "{TAG}" "{work}" "{DAY}"; then echo GAP_OR_NONE; else echo FAILED; fi'
    )
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert "FAILED" in result.stdout
    assert "cannot list the assets" in result.stderr
