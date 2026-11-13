"""Shared fixtures."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import settings

from hockey_model.store.db import connect

# A loaded CI or dev machine makes a 200 ms wall-clock deadline fail a test at random.
# The tests check results, not speed.
settings.register_profile("default", deadline=None)
settings.load_profile("default")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """An empty store that closes after the test."""
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


@pytest.fixture
def fakes(tmp_path: Path) -> Path:
    """Write fake `gh` and `uv` commands. Each records its arguments in calls.log.

    `gh release view TAG` succeeds for a tag in FAKE_RELEASES and otherwise prints
    "release not found", as the real command does. `gh release create` refuses a tag that
    already exists. FAKE_GH_FAIL makes every gh call fail with an auth error.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'echo "gh $*" >> "$FAKE_CALLS"\n'
        'if [[ -n "${FAKE_GH_FAIL:-}" ]]; then echo "auth failed" >&2; exit 4; fi\n'
        'tag="${3:-}"\n'
        'if [[ "$1 $2" == "release view" ]]; then\n'
        '  if grep -qxF "$tag" <<<"${FAKE_RELEASES:-}"; then\n'
        '    if [[ "$*" == *"--json"* ]]; then printf "%s\\n" "${FAKE_ASSETS:-}"; fi\n'
        "    exit 0\n"
        "  fi\n"
        '  echo "release not found" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [[ "$1 $2" == "release create" ]]; then\n'
        '  if grep -qxF "$3" <<<"${FAKE_RELEASES:-}"; then\n'
        '    echo "a release with the same tag name already exists" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "fi\n"
        'if [[ "$1 $2" == "release download" ]]; then\n'
        '  pattern=""; dir=""\n'
        "  while (($# > 0)); do\n"
        '    case "$1" in --pattern) pattern="$2" ;; --dir) dir="$2" ;; esac\n'
        "    shift\n"
        "  done\n"
        '  cp "$FAKE_REMOTE_DIR/$pattern" "$dir/$pattern"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\necho "uv $*" >> "$FAKE_CALLS"\n'
        'if [[ "$*" == "run hockey-model refresh --backfill-from"* ]]; then\n'
        '  if [[ -n "${FAKE_REFRESH_FAIL:-}" ]]; then\n'
        '    printf "{\\"errors\\": [\\"boom\\"]}"; exit 1\n'
        "  fi\n"
        '  printf "{\\"backfill_complete\\": %s}" "${FAKE_BACKFILL_COMPLETE:-true}"\n'
        "fi\n"
        'if [[ "$*" == "run hockey-model report-due"* ]]; then\n'
        '  if [[ -n "${FAKE_UV_FAIL:-}" ]]; then echo "uv boom" >&2; exit 3; fi\n'
        '  printf "%s" "$FAKE_DUE_JSON"\n'
        "fi\n"
        'if [[ "$*" == "run hockey-model report-decide"* ]]; then\n'
        '  case "${FAKE_DECIDE_MODE:-}" in\n'
        '  fail) echo "decide boom" >&2; exit 3 ;;\n'
        "  empty) exit 0 ;;\n"
        '  partial) printf "{\\"due\\": true}"; exit 0 ;;\n'
        "  esac\n"
        '  exec "$FAKE_REAL_UV" "$@"\n'
        "fi\n"
        'if [[ "$*" == "run hockey-model backtest --games-out "* ]]; then\n'
        '  echo "game_id,elo" > "$5"; printf "%s\\n" "$FAKE_BACKTEST_JSON"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for command in (gh, uv):
        command.chmod(0o755)
    return bin_dir
