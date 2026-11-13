"""Shared harness for the report shell script tests: constants, fake command runs, and runners."""

import json
import os
import shutil
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
LIVE = ["xg_form", "rest"]
DAY = "2026-11-04"
TAG = f"reports-{DAY}"
GENERATED = "2026-11-04T20:20:13.000000+00:00"
SHA = "0123456789abcdef0123456789abcdef01234567"
REAL_UV = shutil.which("uv") or ""


def _env(fakes: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CALLS": str(tmp_path / "calls.log"),
        "FAKE_RELEASES": "",
        "FAKE_DUE_JSON": json.dumps(
            {"due": True, "tag": TAG, "date": DAY, "games": 3, "prep_too_early": False}
        ),
        "FAKE_BACKTEST_JSON": json.dumps({"live_features": LIVE}),
        "GITHUB_SHA": SHA,
        # report-decide is the real command: it holds the decision the gate script prints.
        "FAKE_REAL_UV": REAL_UV,
    }
    env.update(extra)
    return env


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
