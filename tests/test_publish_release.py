"""Tests for scripts/publish-release.sh and release.yml, with fake `gh` on PATH (#99)."""

import re
import subprocess
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mutation_copy import require_path
from report_script_support import _calls, _env, _run

ROOT = Path(__file__).resolve().parents[1]
RELEASE = require_path(ROOT / ".github" / "workflows" / "release.yml")
TAG = "v0.1.0"
WHEEL = "hockey_model-0.1.0-py3-none-any.whl"
SDIST = "hockey_model-0.1.0.tar.gz"


def build(tmp_path: Path) -> Path:
    """Write a dist folder, and a notes file next to it. Return the folder."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / WHEEL).write_bytes(b"wheel bytes")
    (dist / SDIST).write_bytes(b"sdist bytes")
    (tmp_path / "notes.md").write_text("- The first release.\n", encoding="utf-8")
    return dist


def publish(
    fakes: Path, tmp_path: Path, *extra: str, **env: str
) -> subprocess.CompletedProcess[str]:
    dist = tmp_path / "dist"
    args = ("--tag", TAG, "--dist", str(dist), "--notes", str(tmp_path / "notes.md"))
    args += extra or ("--prerelease", "false")
    return _run("publish-release.sh", _env(fakes, tmp_path, **env), *args)


def test_a_new_tag_gets_a_release_with_both_files_and_the_notes(
    fakes: Path, tmp_path: Path
) -> None:
    build(tmp_path)
    result = publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    create = [c for c in _calls(tmp_path) if c.startswith("gh release create")]
    assert len(create) == 1
    assert f"gh release create {TAG} " in create[0]
    assert WHEEL in create[0] and SDIST in create[0]
    assert "--verify-tag" in create[0]
    assert "--notes-file" in create[0] and "--prerelease" not in create[0]


def test_a_prerelease_flag_reaches_the_release(fakes: Path, tmp_path: Path) -> None:
    build(tmp_path)
    result = publish(fakes, tmp_path, "--prerelease", "true")
    assert result.returncode == 0, result.stderr
    create = [c for c in _calls(tmp_path) if c.startswith("gh release create")]
    assert "--prerelease" in create[0]


def remote(tmp_path: Path, wheel: bytes = b"wheel bytes", sdist: bytes = b"sdist bytes") -> str:
    folder = tmp_path / "remote"
    folder.mkdir()
    (folder / WHEEL).write_bytes(wheel)
    (folder / SDIST).write_bytes(sdist)
    return str(folder)


def test_a_retry_of_the_same_build_uploads_nothing(fakes: Path, tmp_path: Path) -> None:
    build(tmp_path)
    result = publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"{WHEEL}\n{SDIST}",
        FAKE_REMOTE_DIR=remote(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert not any(c.startswith(("gh release upload", "gh release create")) for c in calls)
    assert result.stderr.count("with the same bytes; skipped") == 2


def test_a_missing_file_is_uploaded_to_an_existing_release(fakes: Path, tmp_path: Path) -> None:
    build(tmp_path)
    result = publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=WHEEL,
        FAKE_REMOTE_DIR=remote(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    upload = [c for c in _calls(tmp_path) if c.startswith("gh release upload")]
    assert len(upload) == 1 and SDIST in upload[0] and WHEEL not in upload[0]


@pytest.mark.parametrize(
    ("wheel", "sdist", "name"),
    [(b"other", b"sdist bytes", WHEEL), (b"wheel bytes", b"other", SDIST)],
)
def test_a_file_with_other_bytes_stops_the_run_before_any_upload(
    fakes: Path, tmp_path: Path, wheel: bytes, sdist: bytes, name: str
) -> None:
    build(tmp_path)
    result = publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"{WHEEL}\n{SDIST}",
        FAKE_REMOTE_DIR=remote(tmp_path, wheel, sdist),
    )
    assert result.returncode == 1
    assert f"{name} is already on {TAG} with other bytes" in result.stderr
    assert not any(c.startswith("gh release upload") for c in _calls(tmp_path))


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FAKE_DOWNLOAD_FAIL": "1"}, f"cannot download {WHEEL} from {TAG}"),
        ({"FAKE_DOWNLOAD_EMPTY": "1"}, f"the download of {WHEEL} from {TAG} is empty"),
    ],
)
def test_a_failed_download_is_not_called_other_bytes(
    fakes: Path, tmp_path: Path, env: dict[str, str], message: str
) -> None:
    build(tmp_path)
    result = publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"{WHEEL}\n{SDIST}",
        FAKE_REMOTE_DIR=remote(tmp_path, b"wheel bytes", b"sdist bytes"),
        **env,
    )
    assert result.returncode == 1
    assert message in result.stderr
    assert "other bytes" not in result.stderr
    assert "Nothing was uploaded" in result.stderr
    assert not any(c.startswith("gh release upload") for c in _calls(tmp_path))


def test_a_collision_stops_the_run_even_when_another_file_is_new(
    fakes: Path, tmp_path: Path
) -> None:
    build(tmp_path)
    result = publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=SDIST,
        FAKE_REMOTE_DIR=remote(tmp_path, sdist=b"other"),
    )
    assert result.returncode == 1
    assert not any(c.startswith("gh release upload") for c in _calls(tmp_path))


def test_a_failed_lookup_is_not_read_as_a_missing_release(fakes: Path, tmp_path: Path) -> None:
    build(tmp_path)
    result = publish(fakes, tmp_path, FAKE_GH_FAIL="1")
    assert result.returncode == 1
    assert "cannot list the releases of the repository" in result.stderr
    assert not any(c.startswith("gh release create") for c in _calls(tmp_path))


def test_a_token_that_cannot_see_the_repository_creates_nothing(
    fakes: Path, tmp_path: Path
) -> None:
    build(tmp_path)
    result = publish(fakes, tmp_path, FAKE_NO_REPO="1")
    assert result.returncode == 1
    assert "the release lookup for v0.1.0 failed" in result.stderr
    assert not any(c.startswith("gh release create") for c in _calls(tmp_path))


@pytest.mark.parametrize("tag", ["0.1.0", "v", "reports-2026-11-04", "v0.1.0; rm", ""])
def test_a_tag_that_is_not_a_release_tag_exits_2(fakes: Path, tmp_path: Path, tag: str) -> None:
    build(tmp_path)
    args = ("--tag", tag, "--dist", str(tmp_path / "dist"), "--notes", str(tmp_path / "notes.md"))
    args += ("--prerelease", "false")
    result = _run("publish-release.sh", _env(fakes, tmp_path), *args)
    assert result.returncode == 2
    assert "is not a release tag" in result.stderr
    assert _calls(tmp_path) == []


def test_a_dist_with_the_wrong_file_count_exits_1(fakes: Path, tmp_path: Path) -> None:
    dist = build(tmp_path)
    (dist / SDIST).unlink()
    result = publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "one wheel and one source archive" in result.stderr


def test_a_missing_notes_file_or_dist_exits_2(fakes: Path, tmp_path: Path) -> None:
    build(tmp_path)
    (tmp_path / "notes.md").unlink()
    assert publish(fakes, tmp_path).returncode == 2
    result = _run("publish-release.sh", _env(fakes, tmp_path), "--tag", TAG, "--dist", "none")
    assert result.returncode == 2


def test_help_and_a_bad_argument(fakes: Path, tmp_path: Path) -> None:
    helped = _run("publish-release.sh", _env(fakes, tmp_path), "--help")
    assert helped.returncode == 0 and "Usage: publish-release.sh" in helped.stdout
    bad = _run("publish-release.sh", _env(fakes, tmp_path), "--nope")
    assert bad.returncode == 2 and "unknown argument" in bad.stderr


def _job(name: str) -> str:
    text = RELEASE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", text, re.DOTALL | re.MULTILINE)
    assert match is not None, f"release.yml has no job named {name}"
    return "".join(
        line
        for line in match.group(1).splitlines(keepends=True)
        if not line.lstrip().startswith("#")
    )


def test_the_workflow_runs_only_for_a_v_tag() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert re.search(r'^on:\n  push:\n    tags: \["v\*"\]\n', text, re.MULTILINE)
    assert "branches:" not in text and "pull_request" not in text


def test_the_jobs_wait_for_each_other_in_order() -> None:
    assert "needs:" not in _job("verify")
    assert "needs: verify" in _job("build")
    assert "needs: [verify, build]" in _job("publish")


def test_only_the_publish_job_can_write() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert re.search(r"^permissions: \{\}$", text, re.MULTILINE)
    assert "contents: write" in _job("publish")
    assert "contents: write" not in _job("verify") + _job("build")
    assert text.count("contents: write") == 1


def test_the_tag_reaches_the_shell_through_an_environment_variable() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert "github.ref_name" in text
    for line in text.splitlines():
        if line.lstrip().startswith("run:") or line.lstrip().startswith("- run:"):
            assert "${{" not in line, f"a run line interpolates an expression: {line.strip()}"
    assert "TAG: ${{ github.ref_name }}" in _job("verify")


def test_every_action_is_pinned_to_a_commit_with_a_version_comment() -> None:
    uses = re.findall(r"^\s*- uses: (\S+)(.*)$", RELEASE.read_text(encoding="utf-8"), re.M)
    assert uses
    for ref, rest in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref
        assert re.search(r"# v[0-9]+\.[0-9]+\.[0-9]+", rest), ref


def test_the_publish_job_runs_the_publish_script_and_no_package_index() -> None:
    publish_job = _job("publish")
    assert "scripts/publish-release.sh" in publish_job
    assert "pypi" not in RELEASE.read_text(encoding="utf-8").lower()


def test_the_verify_job_refuses_a_tag_on_a_commit_that_is_not_on_main() -> None:
    job = _job("verify")
    assert "fetch-depth: 0" in job
    assert 'git merge-base --is-ancestor "$SHA" origin/main' in job
    assert "SHA: ${{ github.sha }}" in job


def test_the_gate_step_fails_when_the_check_fails() -> None:
    """A pipe into tee takes the exit code of tee, unless pipefail is set."""
    step = _job("verify")
    assert "set -o pipefail" in step
    assert step.index("set -o pipefail") < step.index("check_release.py")


def test_the_publish_step_passes_the_prerelease_value_from_the_gate() -> None:
    job = _job("publish")
    assert "PRERELEASE: ${{ needs.verify.outputs.prerelease }}" in job
    assert '--prerelease "$PRERELEASE"' in job


@pytest.mark.parametrize("value", ["", "yes", "1", "True"])
def test_a_prerelease_value_that_is_not_true_or_false_exits_2(
    fakes: Path, tmp_path: Path, value: str
) -> None:
    build(tmp_path)
    result = publish(fakes, tmp_path, "--prerelease", value)
    assert result.returncode == 2
    assert "is not true or false" in result.stderr
    assert not any(c.startswith("gh release") for c in _calls(tmp_path))


def test_a_missing_prerelease_option_exits_2(fakes: Path, tmp_path: Path) -> None:
    build(tmp_path)
    args = ("--tag", TAG, "--dist", str(tmp_path / "dist"), "--notes", str(tmp_path / "notes.md"))
    result = _run("publish-release.sh", _env(fakes, tmp_path), *args)
    assert result.returncode == 2


def test_a_draft_release_for_the_tag_stops_the_run_before_any_write(
    fakes: Path, tmp_path: Path
) -> None:
    build(tmp_path)
    result = publish(fakes, tmp_path, FAKE_DRAFTS=f"v0.0.9\n{TAG}\n")
    assert result.returncode == 1
    assert f"a draft release for {TAG} exists" in result.stderr
    assert not any(
        c.startswith(("gh release create", "gh release upload")) for c in _calls(tmp_path)
    )


def test_a_full_release_list_stops_the_run(fakes: Path, tmp_path: Path) -> None:
    """A list that holds exactly the limit may hide a draft past it.

    One release a day reaches 1000 in about three years, and a draft that the list cuts off
    would be missed. A full list is not an answer, so the run stops and names the limit.
    """
    build(tmp_path)
    full = "\n".join(f"v0.0.{index}" for index in range(1000))
    result = publish(fakes, tmp_path, FAKE_DRAFTS=full)
    assert result.returncode == 1
    assert "1000" in result.stderr
    assert not any(
        c.startswith(("gh release create", "gh release upload")) for c in _calls(tmp_path)
    )


def test_a_draft_for_another_tag_does_not_stop_the_run(fakes: Path, tmp_path: Path) -> None:
    build(tmp_path)
    result = publish(fakes, tmp_path, FAKE_DRAFTS="v0.0.9\n")
    assert result.returncode == 0, result.stderr


def test_an_existing_release_keeps_its_notes_and_the_run_says_so(
    fakes: Path, tmp_path: Path
) -> None:
    build(tmp_path)
    result = publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"{WHEEL}\n{SDIST}",
        FAKE_REMOTE_DIR=remote(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "its notes and pre-release flag stay as they are" in result.stderr
    assert not any(c.startswith("gh release edit") for c in _calls(tmp_path))


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    on_release=st.fixed_dictionaries({WHEEL: st.booleans(), SDIST: st.booleans()}),
    same=st.fixed_dictionaries({WHEEL: st.booleans(), SDIST: st.booleans()}),
)
def test_the_publish_rule_holds_for_any_set_of_files_already_on_the_release(
    fakes: Path,
    tmp_path_factory: pytest.TempPathFactory,
    on_release: dict[str, bool],
    same: dict[str, bool],
) -> None:
    work = tmp_path_factory.mktemp("rule")
    build(work)
    remote_dir = work / "remote"
    remote_dir.mkdir()
    for name, body in ((WHEEL, b"wheel bytes"), (SDIST, b"sdist bytes")):
        (remote_dir / name).write_bytes(body if same[name] else b"other")
    names = [name for name, there in on_release.items() if there]
    result = publish(
        fakes,
        work,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS="\n".join(names),
        FAKE_REMOTE_DIR=str(remote_dir),
    )
    calls = _calls(work)
    uploads = [c for c in calls if c.startswith("gh release upload")]
    collision = any(same[name] is False for name in names)
    assert (result.returncode == 1) == collision
    if collision:
        assert uploads == []
        return
    missing = [name for name in (WHEEL, SDIST) if name not in names]
    assert (len(uploads) == 1) == bool(missing)
    for name in missing:
        assert name in uploads[0]
    for name in names:
        assert not uploads or name not in uploads[0]


@pytest.mark.parametrize(
    ("write", "step"),
    [
        ("create", "the creation of the release v0.1.0"),
        ("upload", "the upload of 2 file(s) to v0.1.0"),
    ],
)
def test_a_failed_write_names_its_step_and_exits_1(
    fakes: Path, tmp_path: Path, write: str, step: str
) -> None:
    build(tmp_path)
    # An existing release makes the run upload; a missing one makes it create (#348).
    existing = TAG if write == "upload" else ""
    result = publish(fakes, tmp_path, FAKE_WRITE_FAIL=write, FAKE_RELEASES=f"{existing}\n")
    assert result.returncode == 1
    assert f"publish-release: {step} failed" in result.stderr


def test_a_failed_write_with_no_draft_deletes_nothing(fakes: Path, tmp_path: Path) -> None:
    # A failed write that left no draft must not delete a release or a tag (#350).
    build(tmp_path)
    result = publish(fakes, tmp_path, FAKE_WRITE_FAIL="create")
    assert result.returncode == 1
    assert not any(c.startswith("gh release delete") for c in _calls(tmp_path))
