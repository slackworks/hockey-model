#!/usr/bin/env bash
# Print the start time of the daily-prepare run that holds the store group now.
#
# Usage: prep-running.sh [--help]
#
# Prints one ISO 8601 UTC instant and exits 0, or prints nothing and exits 0 when no
# daily-prepare run holds the group. The caller needs the start, not a yes or no: GitHub
# counts a job timeout from the job start, so a prepare that started late can hold the
# group past the first puck drop, and one that started on time cannot (#163).
#
# For a running run this script prints the start of its job, read from the job list. A run
# can be `in_progress` while its job still waits for a runner, and the job start is then
# later than the run start. GitHub counts the timeout from the job start, so the job start
# is the one that answers the question. When no job of the run has started, the script
# prints the current time.
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

# Five states can hold the group. `in_progress` runs it. `pending` waits for the group, which
# is the case when a report build job holds it. `queued`, `waiting`, and `requested` wait for a
# runner or for approval, and each holds the group once it starts (#163).
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
statuses=(in_progress pending queued waiting requested)

# Print the id of each daily-prepare run that the pages hold, one per line. Each page is a
# separate JSON object, so the caller passes the whole stream through jq with -s. A list that
# jq cannot read prints nothing and fails, so the caller never reads a bad payload as no
# prepare.
prepare_ids() {
  local answer
  if ! answer="$(jq -rs '
    .[].workflow_runs[]
    | select((.path // "") | endswith("/daily-prepare.yml"))
    | .id
  ' 2>&1)"; then
    echo "prep-running: cannot read the workflow run list: $answer" >&2
    return 1
  fi
  printf '%s' "$answer"
}

# Print the start of the earliest job of run $1, or the current time when no job has started.
# GitHub counts a job timeout from the job start, so this is the instant the window opens.
job_start() {
  local run_id="$1" answer
  if ! answer="$(gh api --paginate \
    "repos/{owner}/{repo}/actions/runs/${run_id}/jobs?per_page=100" 2>&1)"; then
    echo "prep-running: cannot read the jobs of run ${run_id}: $answer" >&2
    return 1
  fi
  if ! answer="$(jq -rs --arg now "$now" '
    [.[].jobs[] | .started_at | select(. != null)] | min // $now
  ' <<<"$answer" 2>&1)"; then
    echo "prep-running: cannot read the job list of run ${run_id}: $answer" >&2
    return 1
  fi
  printf '%s' "$answer"
}

# Print the answer for one status, or nothing when that status holds no prepare run. A status
# that holds a prepare run breaks the caller's loop, because an earlier status holds the group
# first. ISO 8601 UTC strings compare correctly as text while both sides end in `Z`.
status_answer() {
  local status="$1" runs ids id start earliest=""
  if ! runs="$(gh api --paginate \
    "repos/{owner}/{repo}/actions/runs?status=${status}&per_page=100" 2>&1)"; then
    echo "prep-running: cannot read the ${status} workflow runs: $runs" >&2
    return 1
  fi
  if ! ids="$(prepare_ids <<<"$runs")"; then
    return 1
  fi
  if [[ -z "$ids" ]]; then
    return 0
  fi
  # Neither state has started, so each takes the current time.
  if [[ "$status" != "in_progress" ]]; then
    printf '%s' "$now"
    return 0
  fi
  while read -r id; do
    [[ -n "$id" ]] || continue
    start="$(job_start "$id")" || return 1
    if [[ -z "$earliest" || "$start" < "$earliest" ]]; then
      earliest="$start"
    fi
  done <<<"$ids"
  printf '%s' "$earliest"
}

answer=""
for status in "${statuses[@]}"; do
  if ! found="$(status_answer "$status")"; then
    exit 2
  fi
  if [[ -n "$found" ]]; then
    answer="$found"
    break
  fi
done
printf '%s' "$answer"
