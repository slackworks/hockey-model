#!/usr/bin/env bash
# Print the start time of the earliest daily-prepare run that holds the store group now.
#
# Usage: prep-running.sh [--help]
#
# Prints one ISO 8601 UTC instant and exits 0, or prints nothing and exits 0 when no
# daily-prepare run holds the group. The caller needs the start, not a yes or no: GitHub
# counts a job timeout from the job start, so a prepare that started late can hold the
# group past the first puck drop, and one that started on time cannot (#163).
#
# A run that the group holds has not started. GitHub calls that state `pending`, and `queued`
# means it waits for a runner. This script prints the current time for both, because that is
# the earliest instant that the run can start. The run object does carry `run_started_at` in
# those states, but the value is its creation time, and that would read as a run that ends
# early enough to leave the report room.
#
# Exits 2 when the answer is not known: no run list, no gh, or a list that cannot be read.
# A caller MUST NOT read a failed lookup as "no prepare": a wrong answer of none lets the
# report start behind a live prepare run.
#
# Reads the repository from the environment, as `release-exists.sh` does.
set -euo pipefail

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

while (($# > 0)); do
  case "$1" in
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "prep-running: unknown argument '$1'; see --help" >&2
    exit 2
    ;;
  esac
done

if ! command -v gh >/dev/null; then
  echo "prep-running: gh is not installed, so the prepare runs cannot be read" >&2
  exit 2
fi

# Three states can hold the group. `in_progress` runs it. `pending` waits for the group, which
# is the case when a report build job holds it. `queued` waits for a runner, and it holds the
# group once it starts (#163).
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
statuses=(in_progress pending queued)

# Print the earliest start of a daily-prepare run in one list, or "" when the list holds
# none. The run object names its start `run_started_at`; `started_at` is a different field on
# a job, and a run does not carry it at all. Only a running run has started, so the other
# states take $now, the earliest instant that they can start. jq must not run with -e: an
# empty answer is an answer. A list that jq cannot read prints a diagnostic and fails, so the
# caller never reads a bad payload as no prepare.
earliest_prepare() {
  local json="$1" started="$2" answer
  if ! answer="$(jq -r --arg now "$now" --arg started "$started" '
    [.workflow_runs[]
      | select((.path // "") | endswith("/daily-prepare.yml"))
      | (if $started == "1" then (.run_started_at // $now) else $now end)] | min // ""
  ' <<<"$json" 2>&1)"; then
    echo "prep-running: cannot read the workflow run list: $answer" >&2
    return 1
  fi
  printf '%s' "$answer"
}

# The earliest candidate wins, because it can hold the group longest. An empty side loses.
answer=""
for status in "${statuses[@]}"; do
  if ! runs="$(gh api "repos/{owner}/{repo}/actions/runs?status=${status}&per_page=100" 2>&1)"; then
    echo "prep-running: cannot read the ${status} workflow runs: $runs" >&2
    exit 2
  fi
  started=0
  [[ "$status" == "in_progress" ]] && started=1
  found="$(earliest_prepare "$runs" "$started")" || exit 2
  if [[ -n "$found" ]]; then
    answer="$found"
    break
  fi
done
printf '%s' "$answer"
