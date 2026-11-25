"""The release check of scripts/release-gap.sh (#341)."""

import json
import subprocess
from pathlib import Path

from report_script_support import DAY, SCRIPTS, TAG, _env

NEWEST_AT = "2026-11-04T10:00:00.000000+00:00"


def _gap(fakes: Path, tmp_path: Path, newest_file: str, assets: list[str]) -> str:
    """Run release_gap over a fake release whose index names ``newest_file``; return its answer."""
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
        FAKE_REMOTE_DIR=str(remote),
    )
    script = f'source "{SCRIPTS}/release-gap.sh"; release_gap "{TAG}" "{work}" "{DAY}"'
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


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
