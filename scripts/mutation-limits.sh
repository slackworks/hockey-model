#!/usr/bin/env bash
# Print the time limits of the mutation shards, as lines for GITHUB_OUTPUT.
#
# Usage: mutation-limits.sh [STEP_MINUTES]
#
# STEP_MINUTES is the number of minutes that one shard may run mutmut. An empty value or no value
# gives the scheduled limit of 60 minutes. A whole number from 1 to 345 gives that step limit.
# The job limit is always 15 minutes longer than the step limit, for the setup and the upload of
# the results. GitHub stops a job at 360 minutes.
#
# Output (stdout):
#   step_minutes=<minutes>
#   job_minutes=<minutes>
#
# A value that is not a whole number from 1 to 345 prints a message on stderr and exits 2.
# Diagnostics go to stderr, so the output is safe to append to GITHUB_OUTPUT.
set -euo pipefail

readonly DEFAULT_STEP=60
readonly MAX_STEP=345
readonly JOB_MARGIN=15

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

if [[ $# -gt 1 ]]; then
  echo "mutation-limits: expected at most one argument, got $#. Pass STEP_MINUTES or nothing." >&2
  exit 2
fi

value="${1:-}"
if [[ -z "$value" ]]; then
  value="$DEFAULT_STEP"
# The pattern stops a sign, a space, a unit, and a leading zero before the range check.
elif [[ ! "$value" =~ ^[1-9][0-9]{0,2}$ ]] || ((value > MAX_STEP)); then
  echo "mutation-limits: step_minutes must be a whole number of minutes from 1 to $MAX_STEP," \
    "got '$value'. Pass a number, or leave it empty for $DEFAULT_STEP." >&2
  exit 2
fi

echo "step_minutes=$value"
echo "job_minutes=$((value + JOB_MARGIN))"
