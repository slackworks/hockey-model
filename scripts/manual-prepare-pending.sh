#!/usr/bin/env bash
# Say whether a manual daily-prepare run waits for the store group or holds it.
#
# Usage: manual-prepare-pending.sh [WORKFLOW] [--help]
#   WORKFLOW  File name of the workflow to read, such as daily-prepare.yml. The default is
#             daily-prepare.yml. A value that holds a slash is refused.
#
# Prints `yes` when a run of that workflow that a person started (event workflow_dispatch) is
# pending, queued, waiting, requested, or running. Prints `no` otherwise. A scheduled run
# calls this before it joins its concurrency group (#285, #296). GitHub keeps one waiting run
# for a group, and a newer waiting run replaces an older one, so a scheduled run that joined
# the group would cancel the manual run that waits there.
#
# Exits 2 when the answer is not known: no gh, or a run list that cannot be read. A caller
# MUST NOT read a failed lookup as `no`, because a wrong `no` cancels the manual run.
#
# Reads the repository from the environment, as `prep-running.sh` does.
set -euo pipefail

# shellcheck source=scripts/usage.sh
source "$(dirname "$0")/usage.sh"

workflow="daily-prepare.yml"
# A count of the positional arguments, because a guard that compares the value with the
# default accepts the default twice.
named=0
while (($# > 0)); do
  case "$1" in
  -h | --help)
    usage
    exit 0
    ;;
  -*)
    echo "manual-prepare-pending: unknown argument '$1'; see --help" >&2
    exit 2
    ;;
  *)
    if ((named > 0)); then
      echo "manual-prepare-pending: only one workflow name is allowed; see --help" >&2
      exit 2
    fi
    workflow="$1"
    named=1
    shift
    ;;
  esac
done
# The run list holds the workflow path, such as .github/workflows/daily-prepare.yml. The
# argument is the file name, because the caller names a workflow beside it. A value that holds
# a slash reads no run, and it answers `no` to every caller.
if [[ "$workflow" != *.yml || "$workflow" == */* ]]; then
  echo "manual-prepare-pending: '$workflow' is not a workflow file name such as x.yml," \
    "which holds no slash; see --help" >&2
  exit 2
fi

if ! command -v gh >/dev/null; then
  echo "manual-prepare-pending: gh is not installed, so the prepare runs cannot be read" >&2
  exit 2
fi

# One status per call, because the run list filters by one status. The standard error of gh
# goes to a file, so a warning from gh does not enter the JSON that jq reads.
err="$(mktemp)"
trap 'rm -f "$err"' EXIT
for status in in_progress pending queued waiting requested; do
  if ! runs="$(gh api --paginate \
    "repos/{owner}/{repo}/actions/runs?status=${status}&event=workflow_dispatch&per_page=100" \
    2>"$err")"; then
    echo "manual-prepare-pending: cannot read the ${status} workflow runs: $(cat "$err")" >&2
    exit 2
  fi
  if ! found="$(jq -rs --arg workflow "/${workflow}" '
    [.[].workflow_runs[]
      | select((.path // "") | endswith($workflow))
      | select(.event == "workflow_dispatch")] | length
  ' <<<"$runs" 2>"$err")"; then
    echo "manual-prepare-pending: cannot read the ${status} workflow run list: $(cat "$err")" >&2
    exit 2
  fi
  if [[ "$found" != "0" ]]; then
    echo "yes"
    exit 0
  fi
done
echo "no"
