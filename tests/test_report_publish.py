"""Tests for the publish-daily-report.sh script, with fake `gh` on PATH."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mutation_copy import require_path
from report_script_support import (
    DAY,
    SHA,
    TAG,
    _calls,
    _env,
    _run,
    report_game,
    valid_report,
)

from hockey_model.report_schema import add_build
from hockey_model.store.timestamps import iso_utc

require_path(Path(__file__).resolve().parents[1] / "scripts")


def _write_report(report_dir: Path, day: str = DAY, games: tuple[dict, ...] = ()) -> None:
    report_dir.mkdir()
    (report_dir / f"report-{day}.md").write_text("# report\n", encoding="utf-8")
    (report_dir / f"report-{day}.json").write_text(
        json.dumps(valid_report(date=day, games=list(games))) + "\n", encoding="utf-8"
    )
    (report_dir / f"backtest-{day}.json").write_text("{}\n", encoding="utf-8")
    (report_dir / f"backtest-games-{day}.csv").write_text("game_id,elo\n1,1\n", encoding="utf-8")


def _publish(fakes: Path, tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    args = ("--date", DAY, "--report-dir", str(tmp_path / "report"))
    return _run("publish-daily-report.sh", _env(fakes, tmp_path, **extra), *args)


def test_publish_creates_the_release_at_the_build_commit(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    create = [line for line in _calls(tmp_path) if line.startswith("gh release create")]
    assert create and f" {TAG} " in create[0]
    assert f"--target {SHA}" in create[0]
    assert create[0].count("backtest") == 2


def test_only_the_release_calls_see_the_write_token(fakes: Path, tmp_path: Path) -> None:
    """The package commands must never hold the write token, and the release calls need it.

    A package command makes HTTP reads of its own, so a token in its environment is a token
    that any library in the process can read. The script unsets the export once and gives the
    token back to each gh call alone (#313).
    """
    _write_report(tmp_path / "report")
    log = tmp_path / "env.log"
    result = _publish(fakes, tmp_path, FAKE_ENV_LOG=str(log), GH_TOKEN="a-token")
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    uv_calls = [line for line in lines if line.startswith("uv ")]
    assert uv_calls, "the publish made no uv call"
    assert all(" GH_TOKEN=unset " in line for line in uv_calls), uv_calls
    gh_calls = [line for line in lines if line.startswith("gh ")]
    assert gh_calls, lines
    assert all(" GH_TOKEN=a-token " in line for line in gh_calls), gh_calls


def test_publish_accepts_a_leap_day(fakes: Path, tmp_path: Path) -> None:
    day = "2024-02-29"
    report_dir = tmp_path / "report"
    _write_report(report_dir, day)
    result = _run(
        "publish-daily-report.sh",
        _env(fakes, tmp_path),
        "--date",
        day,
        "--report-dir",
        str(report_dir),
    )
    assert result.returncode == 0, result.stderr
    assert any(f"gh release create reports-{day} " in call for call in _calls(tmp_path))


def test_publish_replaces_the_files_of_an_existing_release(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    upload = [line for line in calls if line.startswith("gh release upload")]
    # The alias and the backtest files move to this build with --clobber. The version does not.
    alias = [line for line in upload if "--clobber" in line]
    assert alias and f" {TAG} " in alias[0] and f"/report-{DAY}.json" in alias[0]
    assert any(line.startswith("gh release edit") for line in calls)
    assert not any(line.startswith("gh release create") for line in calls)


def test_publish_notes_say_every_build_is_kept(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    notes = (tmp_path / "report" / f"release-notes-{DAY}.md").read_text(encoding="utf-8")
    assert "Every build's report is kept" in notes
    assert "report-" + DAY + "-HHMMSSZ" in notes
    assert f"commit {SHA}" in notes


def _notes(tmp_path: Path) -> str:
    return (tmp_path / "report" / f"release-notes-{DAY}.md").read_text(encoding="utf-8")


def test_publish_notes_open_with_one_line_for_each_game(fakes: Path, tmp_path: Path) -> None:
    games = (report_game(), report_game("BOS", "NYR", home_win=None, skipped="game is postponed"))
    _write_report(tmp_path / "report", games=games)
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    lines = _notes(tmp_path).splitlines()
    assert "model uncertainty only" in lines[0]
    assert lines[1:3] == [
        "TOR at MTL: TOR 58% (52\u201364%)",
        "NYR at BOS: no prediction (game is postponed)",
    ]
    assert lines[3] == ""
    assert lines[4].startswith("Every build's report is kept")


def test_publish_notes_of_a_day_without_games_say_so(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    assert _publish(fakes, tmp_path).returncode == 0
    assert _notes(tmp_path).startswith("No games start on this day.\n\nEvery build's report")


def test_publish_of_a_rebuild_replaces_the_summary_in_the_notes(
    fakes: Path, tmp_path: Path
) -> None:
    _write_report(tmp_path / "report", games=(report_game(home_win=0.30, interval=(0.2, 0.4)),))
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    assert result.returncode == 0, result.stderr
    notes = _notes(tmp_path)
    assert "TOR at MTL: TOR 70% (60\u201380%)" in notes
    edit = [line for line in _calls(tmp_path) if line.startswith("gh release edit")]
    assert edit and f"release-notes-{DAY}.md" in edit[0]


def test_publish_stops_before_any_upload_when_the_summary_cannot_be_built(
    fakes: Path, tmp_path: Path
) -> None:
    _write_report(tmp_path / "report", games=(report_game(),))
    result = _publish(fakes, tmp_path, FAKE_SUMMARY_FAIL="1")
    assert result.returncode == 1
    assert "cannot build the summary" in result.stderr
    assert not any(line.startswith("gh release") for line in _calls(tmp_path))


def test_publish_needs_the_build_commit(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    env = _env(fakes, tmp_path)
    del env["GITHUB_SHA"]
    args = ("--date", DAY, "--report-dir", str(tmp_path / "report"))
    result = _run("publish-daily-report.sh", env, *args)
    assert result.returncode == 2
    assert "GITHUB_SHA" in result.stderr


@pytest.mark.parametrize(
    "bad", ["../2026-11-04", "2026-11-04 extra", "2026-1-4", "tomorrow", "2026-02-30", "0000-01-01"]
)
def test_publish_refuses_a_malformed_date(fakes: Path, tmp_path: Path, bad: str) -> None:
    _write_report(tmp_path / "report")
    args = ("--date", bad, "--report-dir", str(tmp_path / "report"))
    result = _run("publish-daily-report.sh", _env(fakes, tmp_path), *args)
    assert result.returncode == 2
    assert "not a real date YYYY-MM-DD" in result.stderr
    assert not any(line.startswith("gh release create") for line in _calls(tmp_path))


def test_publish_refuses_a_missing_report(fakes: Path, tmp_path: Path) -> None:
    (tmp_path / "report").mkdir()
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "missing or empty" in result.stderr
    assert not any(line.startswith("gh release create") for line in _calls(tmp_path))


def test_publish_refuses_an_empty_report(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    (tmp_path / "report" / f"report-{DAY}.json").write_text("", encoding="utf-8")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "missing or empty" in result.stderr


def test_publish_refuses_invalid_json(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    (tmp_path / "report" / f"backtest-{DAY}.json").write_text("<html>", encoding="utf-8")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "is not valid JSON" in result.stderr
    assert not any(line.startswith("gh release create") for line in _calls(tmp_path))


def test_publish_refuses_a_csv_without_a_header(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    (tmp_path / "report" / f"backtest-games-{DAY}.csv").write_text("Not Found\n", encoding="utf-8")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "no CSV header" in result.stderr


def test_publish_help_states_that_a_version_is_never_replaced(fakes: Path, tmp_path: Path) -> None:
    result = _run("publish-daily-report.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "is never replaced" in result.stdout
    assert "--report-dir" in result.stdout


def _calls_with(tmp_path: Path, prefix: str, text: str) -> list[str]:
    return [line for line in _calls(tmp_path) if line.startswith(prefix) and text in line]


def test_publish_keeps_each_build_under_its_generated_seconds(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    assert result.returncode == 0, result.stderr
    for ext in ("md", "json"):
        version = _calls_with(tmp_path, "gh release upload", f"report-{DAY}-202013Z.{ext}")
        # A published version is never replaced, so its upload has no --clobber.
        assert version and "--clobber" not in version[0]


def test_publish_creates_the_release_with_versions_and_the_alias(
    fakes: Path, tmp_path: Path
) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    create = _calls_with(tmp_path, "gh release create", f" {TAG} ")
    assert create
    assert f"report-{DAY}-202013Z.json" in create[0]
    assert f"report-{DAY}-202013Z.md" in create[0]
    assert f"/report-{DAY}.json" in create[0]


@pytest.mark.parametrize("generated", ["", "2026-11-04 20:20:13", "2026-11-04T20:20:13Z"])
def test_publish_refuses_a_report_without_the_generated_time(
    fakes: Path, tmp_path: Path, generated: str
) -> None:
    _write_report(tmp_path / "report")
    (tmp_path / "report" / f"report-{DAY}.json").write_text(
        json.dumps(valid_report(generated)) + "\n", encoding="utf-8"
    )
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "generated_at" in result.stderr
    assert not any(line.startswith("gh release") for line in _calls(tmp_path))


def _remote_versions(tmp_path: Path, body: str) -> Path:
    remote = tmp_path / "remote"
    remote.mkdir()
    for ext in ("md", "json"):
        (remote / f"report-{DAY}-202013Z.{ext}").write_text(body, encoding="utf-8")
    return remote


def test_publish_retry_with_the_same_bytes_skips_the_version(fakes: Path, tmp_path: Path) -> None:
    # A retry after a failed alias upload finds its version already on the release.
    _write_report(tmp_path / "report")
    report = tmp_path / "report"
    remote = tmp_path / "remote"
    remote.mkdir()
    for ext in ("md", "json"):
        # The version is a copy of the build, so the same build has the same bytes.
        build = report / f"report-{DAY}.{ext}"
        (remote / f"report-{DAY}-202013Z.{ext}").write_bytes(build.read_bytes())
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"report-{DAY}-202013Z.md\nreport-{DAY}-202013Z.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 0, result.stderr
    assert "already on" in result.stderr and "same bytes" in result.stderr
    assert not _calls_with(tmp_path, "gh release upload", f"report-{DAY}-202013Z")
    assert _calls_with(tmp_path, "gh release upload", "--clobber")


def test_publish_same_name_with_other_bytes_stops_before_the_alias(
    fakes: Path, tmp_path: Path
) -> None:
    _write_report(tmp_path / "report")
    remote = _remote_versions(tmp_path, "a different build\n")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"report-{DAY}-202013Z.md\nreport-{DAY}-202013Z.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 1
    assert "other bytes" in result.stderr
    assert not _calls_with(tmp_path, "gh release upload", "--clobber")


def _write_build(report_dir: Path, generated: str) -> None:
    report_dir.mkdir(exist_ok=True)
    (report_dir / f"report-{DAY}.md").write_text("# report\n", encoding="utf-8")
    (report_dir / f"report-{DAY}.json").write_text(
        json.dumps(valid_report(generated)) + "\n", encoding="utf-8"
    )
    (report_dir / f"backtest-{DAY}.json").write_text("{}\n", encoding="utf-8")
    (report_dir / f"backtest-games-{DAY}.csv").write_text("game_id,elo\n1,1\n", encoding="utf-8")


# The shared fixture is the fake gh, which holds scripts and no state. Each example gets its own
# working directory, which holds the report and the call log, so no example sees the state of
# another.
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    moment=st.datetimes(
        min_value=datetime(2000, 1, 1), max_value=datetime(2099, 12, 31), timezones=st.just(UTC)
    )
)
def test_publish_names_each_version_by_the_hhmmss_of_its_generated_time(
    fakes: Path, tmp_path_factory: pytest.TempPathFactory, moment: datetime
) -> None:
    # Every time iso_utc can write must give a version name with its own HHMMSS.
    work = tmp_path_factory.mktemp("example")
    _write_build(work / "report", iso_utc(moment))
    result = _publish(fakes, work)
    assert result.returncode == 0, result.stderr
    created = _calls_with(work, "gh release create", "")[-1]
    assert f"report-{DAY}-{moment:%H%M%S}Z.json" in created


def _index(report_dir: Path) -> dict:
    return json.loads((report_dir / f"index-{DAY}.json").read_text(encoding="utf-8"))


def test_publish_uploads_the_index_with_the_first_build(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    create = _calls_with(tmp_path, "gh release create", f" {TAG} ")
    assert f"/index-{DAY}.json" in create[0]
    assert [b["file"] for b in _index(tmp_path / "report")["builds"]] == [
        f"report-{DAY}-202013Z.json"
    ]


def test_publish_adds_a_rebuild_to_the_index_on_the_release(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    earlier = add_build(None, valid_report("2026-11-04T10:00:00.000000+00:00"))
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / f"index-{DAY}.json").write_text(json.dumps(earlier), encoding="utf-8")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"report-{DAY}-100000Z.json\nindex-{DAY}.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 0, result.stderr
    files = [b["file"] for b in _index(tmp_path / "report")["builds"]]
    assert files == [f"report-{DAY}-100000Z.json", f"report-{DAY}-202013Z.json"]
    alias = _calls_with(tmp_path, "gh release upload", "--clobber")
    assert alias and f"/index-{DAY}.json" in alias[0]


def test_publish_starts_an_index_when_the_release_has_none(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    assert result.returncode == 0, result.stderr
    assert len(_index(tmp_path / "report")["builds"]) == 1
    assert not _calls_with(tmp_path, "gh release download", "index-")


def test_publish_stops_before_any_upload_when_the_report_does_not_match_the_schema(
    fakes: Path, tmp_path: Path
) -> None:
    report = tmp_path / "report"
    _write_report(report)
    broken = valid_report()
    del broken["games"]
    (report / f"report-{DAY}.json").write_text(json.dumps(broken) + "\n", encoding="utf-8")
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    assert result.returncode == 1
    assert "'games' is a required property" in result.stderr
    assert "Nothing was published" in result.stderr
    assert not any(line.startswith("gh release upload") for line in _calls(tmp_path))
    assert not any(line.startswith("gh release create") for line in _calls(tmp_path))


def test_publish_stops_when_the_index_on_the_release_is_not_valid(
    fakes: Path, tmp_path: Path
) -> None:
    _write_report(tmp_path / "report")
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / f"index-{DAY}.json").write_text("{}", encoding="utf-8")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"index-{DAY}.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 1
    assert "report_index.schema.json" in result.stderr
    assert not any(line.startswith("gh release upload") for line in _calls(tmp_path))


def test_publish_refuses_a_report_of_another_day(fakes: Path, tmp_path: Path) -> None:
    report = tmp_path / "report"
    _write_report(report)
    other = valid_report(date="2026-11-05")
    (report / f"report-{DAY}.json").write_text(json.dumps(other) + "\n", encoding="utf-8")
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    assert result.returncode == 1
    assert "is for 2026-11-05, not 2026-11-04" in result.stderr
    assert not any(line.startswith("gh release upload") for line in _calls(tmp_path))


def test_publish_checks_both_versions_before_it_uploads_either(fakes: Path, tmp_path: Path) -> None:
    # The Markdown version is new and the JSON version collides. No Markdown file may be left
    # on the release without its JSON.
    _write_report(tmp_path / "report")
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / f"report-{DAY}-202013Z.json").write_text("another build\n", encoding="utf-8")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"report-{DAY}-202013Z.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 1
    assert "other bytes" in result.stderr
    assert not any(line.startswith("gh release upload") for line in _calls(tmp_path))


def test_publish_collision_message_says_how_to_recover(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    remote = _remote_versions(tmp_path, "a different build\n")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"report-{DAY}-202013Z.md\nreport-{DAY}-202013Z.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 1
    assert "Run the report workflow again" in result.stderr


def test_publish_downloads_overwrite_an_earlier_check_file(fakes: Path, tmp_path: Path) -> None:
    # A local rerun finds the files of the first run, and gh refuses to overwrite them.
    _write_report(tmp_path / "report")
    remote = tmp_path / "remote"
    remote.mkdir()
    earlier = add_build(None, valid_report("2026-11-04T10:00:00.000000+00:00"))
    (remote / f"index-{DAY}.json").write_text(json.dumps(earlier), encoding="utf-8")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"index-{DAY}.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 0, result.stderr
    downloads = [line for line in _calls(tmp_path) if line.startswith("gh release download")]
    assert downloads and all("--clobber" in line for line in downloads)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FAKE_DOWNLOAD_FAIL": "1"}, f"cannot download index-{DAY}.json from {TAG}"),
        ({"FAKE_DOWNLOAD_EMPTY": "1"}, f"the download of index-{DAY}.json from {TAG} is empty"),
    ],
)
def test_a_failed_index_download_names_the_index_and_publishes_nothing(
    fakes: Path, tmp_path: Path, env: dict[str, str], message: str
) -> None:
    _write_report(tmp_path / "report")
    result = _publish(
        fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_ASSETS=f"index-{DAY}.json", **env
    )
    assert result.returncode == 1
    assert message in result.stderr
    assert "Nothing was published" in result.stderr
    assert not _calls_with(tmp_path, "gh release upload", "")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FAKE_DOWNLOAD_FAIL": "1"}, "cannot download"),
        ({"FAKE_DOWNLOAD_EMPTY": "1"}, "is empty"),
    ],
)
def test_a_failed_version_download_is_not_called_a_collision(
    fakes: Path, tmp_path: Path, env: dict[str, str], message: str
) -> None:
    _write_report(tmp_path / "report")
    remote = _remote_versions(tmp_path, "a different build\n")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"report-{DAY}-202013Z.md\nreport-{DAY}-202013Z.json",
        FAKE_REMOTE_DIR=str(remote),
        **env,
    )
    assert result.returncode == 1
    assert message in result.stderr
    assert "other bytes" not in result.stderr
    assert not _calls_with(tmp_path, "gh release upload", "")


def _release_with_index(tmp_path: Path, generated: str) -> Path:
    """Put an index with one build of ``generated`` where the fake release download finds it."""
    remote = tmp_path / "remote"
    remote.mkdir()
    index = add_build(None, valid_report(generated))
    (remote / f"index-{DAY}.json").write_text(json.dumps(index), encoding="utf-8")
    return remote


def test_a_retry_of_an_older_build_leaves_the_aliases_with_the_newest_build(
    fakes: Path, tmp_path: Path
) -> None:
    # The local build is 20:20:13. A later build at 21:00:00 is already in the index (#318).
    _write_report(tmp_path / "report")
    remote = _release_with_index(tmp_path, "2026-11-04T21:00:00.000000+00:00")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"index-{DAY}.json\nreport-{DAY}.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 0, result.stderr
    assert f"older than the newest build report-{DAY}-210000Z" in result.stderr
    assert _calls_with(tmp_path, "gh release upload", f"report-{DAY}-202013Z.json")
    index_upload = _calls_with(tmp_path, "gh release upload", f"index-{DAY}.json")
    assert index_upload and "--clobber" in index_upload[0]
    assert not _calls_with(tmp_path, "gh release upload", f"/report-{DAY}.json")
    assert not _calls_with(tmp_path, "gh release upload", f"backtest-{DAY}.json")
    assert not _calls_with(tmp_path, "gh release edit", "")
    published = json.loads((tmp_path / "report" / f"index-{DAY}.json").read_text("utf-8"))
    assert [build["file"] for build in published["builds"]] == [
        f"report-{DAY}-202013Z.json",
        f"report-{DAY}-210000Z.json",
    ]


def test_the_newest_build_moves_the_aliases_and_the_notes(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    remote = _release_with_index(tmp_path, "2026-11-04T10:00:00.000000+00:00")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"index-{DAY}.json\nreport-{DAY}.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 0, result.stderr
    alias = _calls_with(tmp_path, "gh release upload", f"/report-{DAY}.json")
    assert alias and "--clobber" in alias[0]
    assert _calls_with(tmp_path, "gh release edit", TAG)
    notes = (tmp_path / "report" / f"release-notes-{DAY}.md").read_text("utf-8")
    assert f"UTC, report-{DAY}-202013Z." in notes


def test_a_release_from_before_the_index_names_the_builds_it_leaves_out(
    fakes: Path, tmp_path: Path
) -> None:
    _write_report(tmp_path / "report")
    old = f"report-{DAY}-101500Z"
    assets = "\n".join([f"{old}.md", f"{old}.json", f"report-{DAY}.json", f"report-{DAY}.md"])
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_ASSETS=assets)
    assert result.returncode == 0, result.stderr
    assert "::warning::" in result.stderr
    assert f"The earlier builds {old} stay on the release with no index entry" in result.stderr
    notes = (tmp_path / "report" / f"release-notes-{DAY}.md").read_text("utf-8")
    assert f"The builds {old} are from before index-{DAY}.json" in notes


def test_a_release_with_an_index_gives_no_warning(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    remote = _release_with_index(tmp_path, "2026-11-04T10:00:00.000000+00:00")
    assets = "\n".join([f"index-{DAY}.json", f"report-{DAY}-100000Z.json"])
    result = _publish(
        fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_ASSETS=assets, FAKE_REMOTE_DIR=str(remote)
    )
    assert result.returncode == 0, result.stderr
    assert "::warning::" not in result.stderr
