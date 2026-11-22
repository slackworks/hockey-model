#!/usr/bin/env bash
# Say whether a manual daily-prepare run waits for the store group or holds it.
#
# Usage: manual-prepare-pending.sh [--help]
#
# Prints `yes` when a daily-prepare run that a person started (event workflow_dispatch) is
# pending, queued, waiting, requested, or running. Prints `no` otherwise. A scheduled run
# calls this before it joins the store group (#285). GitHub keeps one waiting run for a
# group, and a newer waiting run replaces an older one, so a scheduled run that joined
# the group would cancel the manual run that waits there.
#
# Exits 2 when the answer is not known: no gh, or a run list that cannot be read. A caller
# MUST NOT read a failed lookup as `no`, because a wrong `no` cancels the manual run.
#
# Reads the repository from the environment, as `prep-running.sh` does.
set -euo pipefail

# shellcheck source=scripts/usage.sh
source "$(dirname "$0")/usage.sh"

while (($# > 0)); do
  case "$1" in
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "manual-prepare-pending: unknown argument '$1'; see --help" >&2
    exit 2
    ;;
  esac
done

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
  if ! found="$(jq -rs '
    [.[].workflow_runs[]
      | select((.path // "") | endswith("/daily-prepare.yml"))
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
