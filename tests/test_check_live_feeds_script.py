"""Tests for check-live-feeds.sh, with a fake `uv` on PATH."""

import json
import os
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


def _payload(*gaps: dict[str, object]) -> str:
    return json.dumps({"limit_hours": 24.0, "gaps": list(gaps)})


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
