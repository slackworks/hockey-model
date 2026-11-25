"""Shared harness for the report shell script tests: constants, fake command runs, and runners."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from mutation_copy import require_path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
LIVE = ["xg_form", "rest"]
DAY = "2026-11-04"
TAG = f"reports-{DAY}"
# The start of a daily-prepare run, as `prep-running.sh` prints it.
STARTED = "2026-11-04T18:24:31Z"
GENERATED = "2026-11-04T20:20:13.000000+00:00"
SHA = "0123456789abcdef0123456789abcdef01234567"
MODEL_VERSION = "0123456789abcdef"
REAL_UV = shutil.which("uv") or ""


def valid_report(generated: str = GENERATED, **fields: object) -> dict[str, object]:
    """Return a daily report that passes the report schema. Pass a field to change it."""
    report: dict[str, object] = {
        "schema_version": 1,
        "date": DAY,
        "generated_at": generated,
        "inputs_sha256": "a" * 64,
        "level": 0.8,
        "games": [],
        "credits": [],
        "model": {
            "features": LIVE,
            "fit_sha256": "b" * 64,
            "code_sha": SHA,
            "version": MODEL_VERSION,
        },
        "training_gaps": [],
    }
    report.update(fields)
    return report


def due_json(**fields: object) -> str:
    """Return one ``report-due`` payload, with every field the gate prints filled in.

    Pass a field to change it. A field that the gate does not read is still printed, so this
    builder keeps the shape of the real payload.
    """
    payload: dict[str, object] = {
        "due": True,
        "tag": TAG,
        "date": DAY,
        "games": 3,
        "games_outside_window": 0,
        "first_puck_drop_utc": "2026-11-04T23:00:00.000000+00:00",
        "puck_drop_passed": True,
        "prep_blocks_report": False,
    }
    payload.update(fields)
    return json.dumps(payload)


def _env(fakes: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CALLS": str(tmp_path / "calls.log"),
        "FAKE_RELEASES": "",
        "FAKE_DUE_JSON": due_json(),
        "FAKE_BACKTEST_JSON": json.dumps({"live_features": LIVE}),
        "GITHUB_SHA": SHA,
        # The scripts stop without a token, as CI always sets one. A test that needs none passes "".
        "GH_TOKEN": "a-token",
        "REPORT_GH_TOKEN": "a-token",
        # report-decide is the real command: it holds the decision the gate script prints.
        "FAKE_REAL_UV": REAL_UV,
    }
    env.update(extra)
    return env


def store_path(tmp_path: Path) -> Path:
    """Return the store file that the daily scripts name in a test."""
    return tmp_path / "store" / "store.db"


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    # The guard sits at the read, because a caller names a bare file name and never the
    # scripts path, and a mutation copy holds no scripts directory (#155). A skip here skips
    # tests that cannot run; it never lets one pass.
    path = require_path(SCRIPTS / script)
    return subprocess.run(
        ["bash", str(path), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def report_game(
    home: str = "MTL",
    away: str = "TOR",
    *,
    home_win: float | None = 0.42,
    interval: tuple[float, float] | None = (0.36, 0.48),
    skipped: str | None = None,
) -> dict[str, object]:
    """Return one game of a report JSON, as the report writes it, with the range of both sides."""
    win = None
    if home_win is not None and interval is not None:
        low, high = interval
        win = {"home_low": low, "home_high": high, "away_low": 1 - high, "away_high": 1 - low}
    return {
        "game_id": 1,
        "away": away,
        "home": home,
        "start_utc": "2026-11-04T23:00:00.000000+00:00",
        "state": "FUT",
        "skipped": skipped,
        "home_win": home_win,
        "away_win": None if home_win is None else 1 - home_win,
        "made_after_start": False,
        "missing": [],
        "degraded_inputs": [],
        "training": None,
        "win_interval": win,
    }
