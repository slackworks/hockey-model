#!/usr/bin/env bash
# Read the workflow run list that GitHub holds for a repository.
#
# Usage: source run-list.sh; run_list_init NAME
#        run_list_ids WORKFLOW BRANCH EVENT STATUS
#
# run_list_init makes the file that holds the standard error of gh, and removes it at exit.
# Any gh call in the sourcing script can send its standard error to run_list_err, so a
# warning from gh cannot enter the JSON that jq reads.
#
# run_list_ids prints the id of each run of WORKFLOW that holds STATUS, one per line. It
# keeps the runs on one branch when BRANCH is set, and the runs of one event when EVENT is
# set, so a caller that names either never counts the rest. An empty BRANCH or EVENT keeps
# every value.
#
# It prints nothing and exits 1 when gh fails or jq cannot read a page, and the message
# names the script and the status. A failed lookup is not an empty answer, and a wrong
# empty answer makes a caller act while a run holds the store group.
#
# The five states that a group can hold are in RUN_LIST_STATUSES. in_progress runs it,
# pending waits for the group, and queued, waiting, and requested wait for a runner or for an
# approval (#163).
#
# Reads the repository from the environment, as `release-exists.sh` does.

# The script that sources this file reads the list, so this file never reads it.
# shellcheck disable=SC2034
RUN_LIST_STATUSES=(in_progress pending queued waiting requested)

run_list_init() {
  run_list_name="$1"
  run_list_err="$(mktemp)"
  trap 'rm -f "$run_list_err"' EXIT
}

run_list_ids() {
  local workflow="$1" branch="$2" event="$3" status="$4" query runs answer
  query="repos/{owner}/{repo}/actions/runs?status=${status}&per_page=100"
  if [[ -n "$event" ]]; then
    query="repos/{owner}/{repo}/actions/runs?status=${status}&event=${event}&per_page=100"
  fi
  if ! runs="$(gh api --paginate "$query" 2>"$run_list_err")"; then
    echo "${run_list_name}: cannot read the ${status} workflow runs:" \
      "$(cat "$run_list_err")" >&2
    return 1
  fi
  # Each page is a separate JSON object, so the reader passes the whole stream with -s.
  if ! answer="$(jq -rs --arg workflow "/${workflow}" --arg branch "$branch" \
    --arg event "$event" '
    .[].workflow_runs[]
    | select((.path // "") | endswith($workflow))
    | select($branch == "" or .head_branch == $branch)
    | select($event == "" or .event == $event)
    | .id
  ' <<<"$runs" 2>&1)"; then
    echo "${run_list_name}: cannot read the ${status} workflow run list: $answer" >&2
    return 1
  fi
  printf '%s' "$answer"
}
