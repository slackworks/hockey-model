#!/usr/bin/env bash
# Print the end of the window in which a daily-prepare job holds the store group.
#
# Usage: prep-running.sh --timeout JOB=MINUTES [--timeout JOB=MINUTES ...] [--help]
#   --timeout JOB=MINUTES
#            The timeout-minutes of one job that takes the store group, such as skip=10.
#            Give one for each job that holds the group. A test compares the names with the
#            jobs of daily-prepare.yml, because a name that no job carries matches nothing
#            and the answer reads as no prepare.
#
# Prints an ISO 8601 UTC end and exits 0, or prints nothing and exits 0 when no daily-prepare
# job holds the group. The end is the latest end among the holders, and each holder's window
# is its own start plus its own timeout. GitHub counts a job timeout from the job start, so
# the caller needs that one job's window, and not a start time and a guess (#163).
# A report that starts before the end can reach the puck drop, and one that starts after it
# cannot.
#
# The split prepare has two jobs that take the group (#311). The `skip` job reads the store
# and ends in minutes when the day is already prepared. The `prepare` job runs the rebuild and
# takes up to its own timeout. The group frees only after every holder ends, so the holder
# that ends last is the one that the report waits for, and that holder names the window.
#
# A run can be `in_progress` while its job still waits for a runner, and the job start is then
# later than the run start. GitHub counts the timeout from the job start, so the job start is
# the one that answers the question. When no job of the run has started, the script uses the
# current time. Only the jobs that the --timeout flags name count: the `gate` job of the same
# run starts at once and holds nothing (#285). A job that waits carries a `started_at` of its
# creation time, so the script reads that value only from a job that is `in_progress` or
# `completed`. A job that concluded with `skipped` holds no group.
#
# A run lists its jobs one at a time, so a run that just started lists only its gate job. That
# run has not started its holders, and each holder still holds the group once it appears
# (#311). The script therefore gives such a run the longest window from now, unless its gate
# job has completed. Without that rule the run reads as no prepare only seconds after it
# started, and the report gate lets a replace build run into the puck drop.
#
# A run that the group holds has not started. GitHub calls that state `pending`, and `queued`
# means it waits for a runner. This script uses the current time for both, because that is the
# earliest instant that the run can start. The run object does carry `run_started_at` in
# those states, but the value is its creation time, and that would read as a run that ends
# early enough to leave the report room. Such a run names the longest of the --timeout values,
# because the group can admit its rebuild job first (#311).
#
# Exits 2 when the answer is not known: no run list, no gh, or a list that cannot be read.
# A caller MUST NOT read a failed lookup as "no prepare": a wrong answer of none lets the
# report start behind a live prepare run.
#
# Reads the repository from the environment, as `release-exists.sh` does.
set -euo pipefail

# shellcheck source=scripts/usage.sh
source "$(dirname "$0")/usage.sh"

declare -A TIMEOUTS=()

while (($# > 0)); do
  case "$1" in
  --timeout)
    if (($# < 2)); then
      echo "prep-running: --timeout needs JOB=MINUTES, such as skip=10; see --help" >&2
      exit 2
    fi
    job="${2%%=*}"
    minutes="${2#*=}"
    if [[ "$2" != *=* || -z "$job" ]]; then
      echo "prep-running: --timeout '$2' is not JOB=MINUTES; see --help" >&2
      exit 2
    fi
    if [[ ! "$minutes" =~ ^[0-9]{1,3}$ ]]; then
      echo "prep-running: --timeout '$2' needs whole minutes from 1 to 360, which is the" \
        "GitHub job limit; see --help" >&2
      exit 2
    fi
    # The arithmetic reads a leading zero as base 8, so "08" would stop this script.
    if [[ "$minutes" == 0* ]] || ((minutes < 1 || minutes > 360)); then
      echo "prep-running: --timeout '$2' needs whole minutes from 1 to 360, with no leading" \
        "zero; see --help" >&2
      exit 2
    fi
    TIMEOUTS["$job"]="$minutes"
    shift 2
    ;;
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

if ((${#TIMEOUTS[@]} == 0)); then
  echo "prep-running: give the timeout of each job that holds the group, such as" \
    "--timeout skip=10 --timeout prepare=180; see --help" >&2
  exit 2
fi

if ! command -v gh >/dev/null; then
  echo "prep-running: gh is not installed, so the prepare runs cannot be read" >&2
  exit 2
fi

# The standard error of gh goes to a file, so a warning from gh cannot enter the JSON that
# jq reads, as manual-prepare-pending.sh does.
err="$(mktemp)"
trap 'rm -f "$err"' EXIT

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

# Print a header line, then "<JOB> <START>" for each job of run $1 that the --timeout flags
# name and that holds the group. A job that has not started prints an empty START, because it
# can still use its whole timeout. The holder is a job that GitHub holds just before it starts
# or that has started, so a job with any other state prints no line. The set that holds is
# written out, because each job state that does not hold is a separate state: a skipped job, a
# cancelled job, one that failed before it touched the store, and one that completed hours ago.
#
# The header is `meta <LISTED> <GATE_DONE>`. LISTED says whether the run lists any job that a
# --timeout flag names, and GATE_DONE says whether its `gate` job has completed. The caller
# needs both: a run that lists only its gate job has not started its holders yet, and the
# holders still hold the group once they appear (#311).
#
# A --timeout name that the run does not carry prints no line, and that is not an error. The
# jobs of the split prepare appear one at a time (#311): while the skip job runs, the job list
# holds no `prepare` job, because the rebuild has not started and may never start. A name that
# matches nothing is silent here; a test compares the names with daily-prepare.yml instead.
job_holds() {
  local run_id="$1" answer names
  if ! answer="$(gh api --paginate \
    "repos/{owner}/{repo}/actions/runs/${run_id}/jobs?per_page=100" 2>"$err")"; then
    echo "prep-running: cannot read the jobs of run ${run_id}: $(cat "$err")" >&2
    return 1
  fi
  if ! names="$(printf '%s\n' "${!TIMEOUTS[@]}" | jq -R . | jq -cs . 2>&1)"; then
    echo "prep-running: cannot build the list of job names: $names" >&2
    return 1
  fi
  if ! answer="$(jq -rs --argjson names "$names" '
    def holds: . == "in_progress" or . == "queued" or . == "waiting"
      or . == "pending" or . == "requested";
    def named: .name as $n | $names | index($n);
    [.[].jobs[]] as $jobs
    | "meta \(if any($jobs[]; named) then 1 else 0 end)"
      + " \(if any($jobs[]; .name == "gate" and .status == "completed") then 1 else 0 end)",
      ($jobs[]
        | select(.status | holds)
        | select(named)
        | "\(.name) \(if .status == "in_progress" then (.started_at // "") else "" end)")
  ' <<<"$answer" 2>&1)"; then
    echo "prep-running: cannot read the job list of run ${run_id}: $answer" >&2
    return 1
  fi
  printf '%s' "$answer"
}

# Print the longest of the --timeout values. A run with no job start can reach the group with
# whichever job it admits first, so the longest window is the one to plan for (#311).
longest_timeout() {
  local name longest=0
  for name in "${!TIMEOUTS[@]}"; do
    if ((${TIMEOUTS[$name]} > longest)); then
      longest="${TIMEOUTS[$name]}"
    fi
  done
  printf '%s' "$longest"
}

# Print the instant that a window ends. The window opens at $1 and lasts $2 minutes. A jq
# error goes to stdout, so the caller names the cause with the context it holds.
window_end() {
  jq -rn --arg start "$1" --argjson m "$2" \
    '$start | fromdateiso8601 + ($m * 60) | todateiso8601' 2>&1
}

# Print the latest end of every holder in one run, or nothing when the run has no holder. Both
# holders of the split prepare can be live in one run, so the answer is the holder that ends
# last (#311).
#
# A run that lists no holder job yet holds the group as soon as one appears, and it can reach
# whichever job it admits first. Unless its gate job has completed, the run therefore opens the
# longest window from now. Without this, a run seconds after its start reads as no prepare, and
# the report gate lets a replace build run into the puck drop (#311).
run_answer() {
  local run_id="$1" holds job start end latest_end="" listed="" gate_done=""
  holds="$(job_holds "$run_id")" || return 1
  while read -r job start; do
    [[ -n "$job" ]] || continue
    if [[ "$job" == "meta" ]]; then
      read -r listed gate_done <<<"$start"
      continue
    fi
    # A job that has not started can still use its whole window from now.
    if [[ -z "$start" ]]; then
      start="$now"
    fi
    if ! end="$(window_end "$start" "${TIMEOUTS[$job]}")"; then
      echo "prep-running: cannot add ${TIMEOUTS[$job]} minutes to '${start}', which run" \
        "${run_id} gave as the start of job ${job}: $end" >&2
      return 1
    fi
    if [[ -z "$latest_end" || "$end" > "$latest_end" ]]; then
      latest_end="$end"
    fi
  done <<<"$holds"
  if [[ -z "$latest_end" && "$listed" == 0 && "$gate_done" == 0 ]]; then
    if ! latest_end="$(window_end "$now" "$(longest_timeout)")"; then
      echo "prep-running: cannot add $(longest_timeout) minutes to '${now}', which this" \
        "script read as the current time: $latest_end" >&2
      return 1
    fi
  fi
  printf '%s' "$latest_end"
}

# Print the end of the window of one status, or nothing when that status holds no prepare run.
# A status that holds a prepare run breaks the caller's loop, because an earlier status holds
# the group first. ISO 8601 UTC strings compare correctly as text while both sides end in `Z`.
status_answer() {
  local status="$1" runs ids id answer start end latest_end="" longest=0 name
  if ! runs="$(gh api --paginate \
    "repos/{owner}/{repo}/actions/runs?status=${status}&per_page=100" 2>"$err")"; then
    echo "prep-running: cannot read the ${status} workflow runs: $(cat "$err")" >&2
    return 1
  fi
  if ! ids="$(prepare_ids <<<"$runs")"; then
    return 1
  fi
  if [[ -z "$ids" ]]; then
    return 0
  fi
  # A run that the group holds, or one that waits for a runner or an approval, has no job
  # start to read. Its creation time is not a start, so every window opens now. The group
  # admits whichever job it reaches first, and a forced run reaches the rebuild without a
  # check job, so the longest window is the one that the report must plan for (#311).
  if [[ "$status" != "in_progress" ]]; then
    start="$now"
    for name in "${!TIMEOUTS[@]}"; do
      if ((${TIMEOUTS[$name]} > longest)); then
        longest="${TIMEOUTS[$name]}"
      fi
    done
    if ! end="$(window_end "$start" "$longest")"; then
      echo "prep-running: cannot add ${longest} minutes to '${start}', which this script" \
        "read as the current time: $end" >&2
      return 1
    fi
    printf '%s' "$end"
    return 0
  fi
  while read -r id; do
    [[ -n "$id" ]] || continue
    # run_answer names the cause on stderr, so this level adds nothing.
    answer="$(run_answer "$id")" || return 1
    [[ -n "$answer" ]] || continue
    if [[ -z "$latest_end" || "$answer" > "$latest_end" ]]; then
      latest_end="$answer"
    fi
  done <<<"$ids"
  printf '%s' "$latest_end"
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
