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
# A queued prepare has not started yet. This script prints the current time for it, which
# is the earliest instant it can start, so the caller never reads it as a short run.
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

# The list holds the running runs, and the runs queued behind them (#163).
if ! runs="$(gh api 'repos/{owner}/{repo}/actions/runs?status=in_progress&per_page=100' 2>&1)"; then
  echo "prep-running: cannot read the running workflow runs: $runs" >&2
  exit 2
fi
if ! queued="$(gh api 'repos/{owner}/{repo}/actions/runs?status=queued&per_page=100' 2>&1)"; then
  echo "prep-running: cannot read the queued workflow runs: $queued" >&2
  exit 2
fi

# A queued run has no start time yet, so this is the earliest instant that it can start.
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Print the earliest start of a daily-prepare run in one list, or "" when the list holds
# none. jq must not run with -e: an empty answer is an answer. A list that jq cannot read
# prints a diagnostic and fails, so the caller never reads a bad payload as no prepare.
earliest_prepare() {
  local json="$1" answer
  if ! answer="$(jq -r --arg now "$now" '
    [.workflow_runs[]
      | select(.path | endswith("/daily-prepare.yml"))
      | (.started_at // $now)] | min // ""
  ' <<<"$json" 2>&1)"; then
    echo "prep-running: cannot read the workflow run list: $answer" >&2
    return 1
  fi
  printf '%s' "$answer"
}

running_start="$(earliest_prepare "$runs")" || exit 2
queued_start="$(earliest_prepare "$queued")" || exit 2

# Compare the two candidates as instants, and print the earlier one. An empty side loses.
if [[ -z "$running_start" ]]; then
  printf '%s' "$queued_start"
elif [[ -z "$queued_start" ]]; then
  printf '%s' "$running_start"
else
  printf '%s' "$(printf '%s\n%s\n' "$running_start" "$queued_start" | sort | head -n 1)"
fi
