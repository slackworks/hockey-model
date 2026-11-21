"""Tests for check-live-feeds.sh, with a fake `uv` on PATH."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from mutation_copy import require_path

# The mutation run copies src and tests only, so the script is not there to test.
SCRIPT = require_path(Path(__file__).resolve().parents[1] / "scripts" / "check-live-feeds.sh")


def _gap(source: str, over: bool, age: float = 30.5) -> dict[str, object]:
    return {
        "source": source,
        "url": f"https://example.test/{source}",
        "gap_since": "2026-11-20T06:00:00.000000+00:00",
        "age_hours": age,
        "last_ok": None,
        "over_limit": over,
    }


def _feed(source: str, over: bool = False, newest: str | None = "2026-11-21T11:00:00.000000+00:00"):
    age = None if newest is None else 1.0
    return {"source": source, "newest_read": newest, "age_hours": age, "over_limit": over}


def _fresh_feeds() -> list[dict[str, object]]:
    return [_feed(name) for name in ("espn_starters", "espn_injuries", "daily_faceoff")]


def _payload(*gaps: dict[str, object], feeds: list[dict[str, object]] | None = None) -> str:
    listed = _fresh_feeds() if feeds is None else feeds
    return json.dumps({"limit_hours": 24.0, "gaps": list(gaps), "feeds": listed})


@pytest.fixture
def fake_uv(tmp_path: Path) -> Path:
    """Write a fake `uv` that records its arguments and prints FAKE_GAPS_JSON."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\necho "uv $*" >> "$FAKE_CALLS"\n'
        'if [[ -n "${FAKE_UV_FAIL:-}" ]]; then echo "uv boom" >&2; exit 3; fi\n'
        'printf "%s" "$FAKE_GAPS_JSON"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return bin_dir


def _run(
    tmp_path: Path, fake_uv: Path, gaps_json: str, *args: str, **extra: str
) -> subprocess.CompletedProcess[str]:
    calls = tmp_path / "calls.log"
    calls.touch()
    env = {
        **os.environ,
        "PATH": f"{fake_uv}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CALLS": str(calls),
        "FAKE_GAPS_JSON": gaps_json,
        **extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_no_gap_raises_no_alert(fake_uv: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, fake_uv, _payload())
    assert result.returncode == 0, result.stderr
    assert "no live feed" in result.stdout
    assert (tmp_path / "calls.log").read_text(encoding="utf-8") == (
        "uv run hockey-model store gaps --live\n"
    )


def test_a_gap_that_is_not_over_the_limit_raises_no_alert(fake_uv: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, fake_uv, _payload(_gap("daily_faceoff", over=False, age=3.0)))
    assert result.returncode == 0, result.stderr


def test_a_gap_over_the_limit_names_the_source_the_url_and_the_age(
    fake_uv: Path, tmp_path: Path
) -> None:
    result = _run(tmp_path, fake_uv, _payload(_gap("daily_faceoff", over=True)))
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "::error::check-live-feeds: daily_faceoff has failed since "
        "2026-11-20T06:00:00.000000+00:00, 30.5 hours, over the limit of 24 hours: "
        "https://example.test/daily_faceoff. Check that the host is reachable and does not "
        "block this runner. The next good read clears the gap by itself."
    ]


def test_each_gap_over_the_limit_gets_its_own_line(fake_uv: Path, tmp_path: Path) -> None:
    gaps = _payload(
        _gap("espn_injuries", over=True),
        _gap("daily_faceoff", over=False),
        _gap("espn_starters", over=True),
    )
    result = _run(tmp_path, fake_uv, gaps)
    assert result.returncode == 1
    assert [line.split()[1] for line in result.stderr.splitlines()] == [
        "espn_injuries",
        "espn_starters",
    ]


def test_a_failed_command_stops_the_check(fake_uv: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, fake_uv, "", FAKE_UV_FAIL="1")
    assert result.returncode == 1
    assert "'store gaps --live' failed" in result.stderr
    assert "uv boom" in result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json",
        "[]",
        json.dumps({"limit_hours": 24, "gaps": "none"}),
        json.dumps({"limit_hours": 24, "gaps": []}),
        json.dumps({"limit_hours": 24, "gaps": [], "feeds": [{"source": "daily_faceoff"}]}),
        json.dumps({"gaps": []}),
        json.dumps({"limit_hours": 24, "gaps": [{"source": "daily_faceoff"}]}),
    ],
)
def test_a_payload_of_another_shape_stops_the_check(
    fake_uv: Path, tmp_path: Path, payload: str
) -> None:
    result = _run(tmp_path, fake_uv, payload)
    assert result.returncode == 1
    assert "another shape" in result.stderr


def test_help_prints_the_usage(fake_uv: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, fake_uv, "", "--help")
    assert result.returncode == 0
    assert result.stdout.startswith("Alert when a live feed has stayed a known gap")
    assert (tmp_path / "calls.log").read_text(encoding="utf-8") == ""


def test_an_unknown_argument_is_refused(fake_uv: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, fake_uv, "", "--now")
    assert result.returncode == 2
    assert "unknown argument '--now'" in result.stderr


def test_a_missing_jq_is_named_before_the_command_runs(fake_uv: Path, tmp_path: Path) -> None:
    # Only the fake bin directory is on PATH, so jq is not found.
    result = subprocess.run(
        [shutil.which("bash") or "bash", str(SCRIPT)],
        cwd=tmp_path,
        env={"PATH": str(fake_uv), "FAKE_CALLS": str(tmp_path / "calls.log")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "jq is not on PATH" in result.stderr
    assert not (tmp_path / "calls.log").exists()


def test_a_feed_with_an_old_newest_read_names_the_feed_and_the_read(
    fake_uv: Path, tmp_path: Path
) -> None:
    feeds = [_feed("daily_faceoff", over=True, newest="2026-11-18T10:00:00.000000+00:00")]
    result = _run(tmp_path, fake_uv, _payload(feeds=feeds))
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "::error::check-live-feeds: daily_faceoff has no read in the last 24 hours "
        "(newest read: 2026-11-18T10:00:00.000000+00:00). Check that daily-prepare and "
        "daily-report still run and save the store cache."
    ]


def test_a_feed_that_was_never_read_says_none(fake_uv: Path, tmp_path: Path) -> None:
    feeds = [_feed("espn_injuries", over=True, newest=None)]
    result = _run(tmp_path, fake_uv, _payload(feeds=feeds))
    assert result.returncode == 1
    assert "(newest read: none)" in result.stderr


def test_a_gap_and_a_silent_feed_each_get_their_own_line(fake_uv: Path, tmp_path: Path) -> None:
    feeds = [_feed("espn_starters", over=True, newest=None)]
    result = _run(tmp_path, fake_uv, _payload(_gap("daily_faceoff", over=True), feeds=feeds))
    assert result.returncode == 1
    assert [line.split()[1] for line in result.stderr.splitlines()] == [
        "daily_faceoff",
        "espn_starters",
    ]
