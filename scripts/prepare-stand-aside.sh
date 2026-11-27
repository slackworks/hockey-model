#!/usr/bin/env bash
# Decide whether a daily-prepare run may go on. Writes go=true or go=false to GITHUB_OUTPUT.
#
# Usage: prepare-stand-aside.sh [--help]
#
# The workflow passes the event name in EVENT. A manual run always goes on. A scheduled run
# stands aside while scripts/manual-prepare-pending.sh reports a manual run that waits or runs
# (#285, #335). The answer of that script must be yes or no. A failed lookup, or any other
# answer, stops the run, because a failure must not read as no pending run.
#
# Exits 0 with go written, 1 for a failed lookup or an unknown answer, 2 for bad arguments.
set -euo pipefail

case "${1:-}" in
"") ;;
-h | --help)
  echo "Usage: prepare-stand-aside.sh   (reads EVENT; appends go=true|false to GITHUB_OUTPUT)"
  exit 0
  ;;
*)
  echo "prepare-stand-aside: unknown argument '$1'; see --help" >&2
  exit 2
  ;;
esac

: "${EVENT:?prepare-stand-aside: EVENT is not set; pass the workflow event name}"
: "${GITHUB_OUTPUT:?prepare-stand-aside: GITHUB_OUTPUT is not set; run this in a workflow step}"

if [[ "$EVENT" != "schedule" ]]; then
  echo "go=true" >>"$GITHUB_OUTPUT"
  exit 0
fi

script_dir="$(dirname "$0")"
pending="$("$script_dir/manual-prepare-pending.sh")" || {
  echo "::error::The lookup of manual daily-prepare runs failed, so this run stops." >&2
  exit 1
}
case "$pending" in
no)
  echo "go=true" >>"$GITHUB_OUTPUT"
  ;;
yes)
  echo "::warning::A manual daily-prepare run waits or runs, so this run stands aside." >&2
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    echo "This scheduled run stood aside for a manual daily-prepare run that waits or runs." \
      >>"$GITHUB_STEP_SUMMARY"
  fi
  echo "go=false" >>"$GITHUB_OUTPUT"
  ;;
*)
  echo "::error::The lookup of manual daily-prepare runs answered '$pending', not yes or no." >&2
  exit 1
  ;;
esac
