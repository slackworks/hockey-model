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

    `gh api --include .../releases/tags/TAG` answers 200 for a tag in FAKE_RELEASES and 404
    otherwise, as the real command does. `gh release view TAG` answers the same way for the
    asset read. `gh release create` refuses a tag that already exists. FAKE_GH_FAIL makes
    every gh call fail with an auth error. `gh api .../actions/runs?status=S` answers
    FAKE_RUNS_JSON_S when it is set and FAKE_RUNS_JSON otherwise, which defaults to no runs,
    and it fails when FAKE_RUNS_FAIL is set.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'echo "gh $*" >> "$FAKE_CALLS"\n'
        'echo "gh GH_TOKEN=${GH_TOKEN-unset}" >> "${FAKE_ENV_LOG:-/dev/null}"\n'
        'if [[ -n "${FAKE_GH_FAIL:-}" ]]; then echo "auth failed" >&2; exit 4; fi\n'
        # The mutation report reads the open issue list. The default is an empty list.
        'if [[ "$1 $2" == "issue list" ]]; then printf "%s\\n" "${FAKE_ISSUES:-[]}"; exit 0; fi\n'
        'if [[ "$1" == "api" ]]; then\n'
        '  path="${!#}"\n'
        # The workflow runs, which decide whether a prepare run holds the store group. The list
        # depends on the status, so FAKE_RUNS_JSON_<STATUS> answers one status and
        # FAKE_RUNS_JSON answers the rest. The default goes in its own variable, because a
        # brace in a ${...:-default} word makes bash end the expansion early.
        # The jobs of one run answer FAKE_JOBS_JSON, which defaults to no jobs. The lookup
        # reads the job start, because GitHub counts a job timeout from there.
        '  if [[ "$path" == */actions/runs/*/jobs* ]]; then\n'
        '    if [[ -n "${FAKE_JOBS_FAIL:-}" ]]; then echo "jobs boom" >&2; exit 1; fi\n'
        '    jobs_json="${FAKE_JOBS_JSON:-}"\n'
        '    if [[ -z "$jobs_json" ]]; then jobs_json=\'{"jobs":[]}\'; fi\n'
        '    printf "%s\\n" "$jobs_json"\n'
        "    exit 0\n"
        "  fi\n"
        '  if [[ "$path" == */actions/runs* ]]; then\n'
        '    if [[ -n "${FAKE_RUNS_FAIL:-}" ]]; then echo "runs boom" >&2; exit 1; fi\n'
        '    runs_json=""\n'
        "    for status in in_progress pending queued waiting requested; do\n"
        '      if [[ "$path" == *"status=$status"* ]]; then\n'
        '        var="FAKE_RUNS_JSON_${status^^}"\n'
        '        runs_json="${!var:-}"\n'
        "      fi\n"
        "    done\n"
        '    if [[ -z "$runs_json" ]]; then runs_json="${FAKE_RUNS_JSON:-}"; fi\n'
        '    if [[ -z "$runs_json" ]]; then runs_json=\'{"workflow_runs":[]}\'; fi\n'
        '    printf "%s\\n" "$runs_json"\n'
        "    exit 0\n"
        "  fi\n"
        # The ci runs of a commit answer FAKE_CI_RUNS. It defaults to a list with no run.
        '  if [[ "$path" == */actions/workflows/*/runs* ]]; then\n'
        '    if [[ -n "${FAKE_CI_RUNS_FAIL:-}" ]]; then echo "runs boom" >&2; exit 1; fi\n'
        '    runs_json="${FAKE_CI_RUNS:-}"\n'
        '    if [[ -z "$runs_json" ]]; then runs_json=\'{"workflow_runs":[]}\'; fi\n'
        '    printf "%s\\n" "$runs_json"\n'
        "    exit 0\n"
        "  fi\n"
        # The release lookup reads the repository first, so a token that cannot see the
        # repository is not read as a missing release.
        '  if [[ "$path" != */tags/* ]]; then\n'
        '    if [[ -n "${FAKE_NO_REPO:-}" ]]; then echo "HTTP/2.0 404 Not Found"; exit 1; fi\n'
        '    echo "HTTP/2.0 200 OK"\n'
        "    exit 0\n"
        "  fi\n"
        '  tag="${path##*/tags/}"\n'
        '  if grep -qxF "$tag" <<<"${FAKE_RELEASES:-}"; then echo "HTTP/2.0 200 OK"; exit 0; fi\n'
        '  echo "HTTP/2.0 404 Not Found"\n'
        "  exit 1\n"
        "fi\n"
        'tag="${3:-}"\n'
        'if [[ "$1 $2" == "release view" ]]; then\n'
        '  if grep -qxF "$tag" <<<"${FAKE_RELEASES:-}"; then\n'
        '    if [[ "$*" == *"--json"* ]]; then printf "%s\\n" "${FAKE_ASSETS:-}"; fi\n'
        "    exit 0\n"
        "  fi\n"
        '  echo "release not found" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [[ "$1 $2" == "release list" ]]; then printf "%s\\n" "${FAKE_DRAFTS:-}"; exit 0; fi\n'
        'if [[ "$1 $2" == "release create" ]]; then\n'
        '  if grep -qxF "$3" <<<"${FAKE_RELEASES:-}"; then\n'
        '    echo "a release with the same tag name already exists" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "fi\n"
        'if [[ "$1 $2" == "release download" ]]; then\n'
        '  if [[ -n "${FAKE_DOWNLOAD_FAIL:-}" ]]; then echo "download boom" >&2; exit 1; fi\n'
        '  if [[ -n "${FAKE_DOWNLOAD_EMPTY:-}" ]]; then exit 0; fi\n'
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
        'echo "uv GH_TOKEN=${GH_TOKEN-unset}" >> "${FAKE_ENV_LOG:-/dev/null}"\n'
        # The mutation report runs the real helper, so its status is the real decision. The
        # path is absolute, because a bare name would find this fake again on PATH.
        'if [[ "$*" == "run python3 scripts/mutation_report.py"* ]]; then\n'
        '  exec "$FAKE_REAL_UV" "$@"\n'
        "fi\n"
        'if [[ "$*" == "run hockey-model store upgrade"* ]]; then\n'
        '  if [[ -n "${FAKE_UPGRADE_FAIL:-}" ]]; then echo "upgrade boom" >&2; exit 2; fi\n'
        '  printf "{\\"changed\\": true, \\"to_version\\": 3}"; exit 0\n'
        "fi\n"
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
        # The daily command writes the report JSON that a test names in FAKE_BUILT_REPORT.
        'if [[ "$*" == "run hockey-model daily "* && -n "${FAKE_BUILT_REPORT:-}" ]]; then\n'
        '  printf "%s\\n" "$FAKE_BUILT_REPORT" > "$7/report-$5.json"\n'
        "fi\n"
        # The compare step is the real command: it holds the comparison the script acts on.
        'if [[ "$*" == "run hockey-model report-compare"* ]]; then\n'
        '  exec "$FAKE_REAL_UV" "$@"\n'
        "fi\n"
        # The index step is the real command: it holds the schema check the publish script needs.
        'if [[ "$*" == "run hockey-model report-index"* ]]; then\n'
        '  exec "$FAKE_REAL_UV" "$@"\n'
        "fi\n"
        'if [[ "$*" == "run hockey-model backtest --games-out "* ]]; then\n'
        '  if [[ -n "${FAKE_BACKTEST_FAIL:-}" ]]; then echo "backtest boom" >&2; exit 4; fi\n'
        '  echo "game_id,elo" > "$5"; printf "%s\\n" "$FAKE_BACKTEST_JSON"\n'
        "fi\n"
        'if [[ "$*" == "run hockey-model fit "* && -n "${FAKE_FIT_FAIL:-}" ]]; then\n'
        '  echo "fit boom" >&2; exit 5\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for command in (gh, uv):
        command.chmod(0o755)
    return bin_dir
