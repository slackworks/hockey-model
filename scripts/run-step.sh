#!/usr/bin/env bash
# Run one step of a script and log its progress lines to stderr, in the format of
# src/hockey_model/progress.py. Source this file. It is not a command.
#
# Usage: RUN_STEP_COMMAND=NAME run_step STEP CMD [ARG...]
#   RUN_STEP_COMMAND  The name of the calling script, such as daily-prepare.
#   STEP              One word that names the step, such as backtest.
#
# The function logs "progress NAME STEP start elapsed=0.0s", runs the command, and then logs
# "progress NAME STEP end elapsed=Ns", or "progress NAME STEP stop elapsed=Ns reason=exit code N"
# when the command failed. The command keeps its own stdout and stderr. The function returns
# the exit code of the command, so a caller under "set -e" stops with that code.

run_step() {
  local name="${1:-}"
  if [[ -z "$name" || "$name" =~ [[:space:]] ]]; then
    echo "run-step: the step name '$name' must be one word with no space; use a dash" >&2
    return 2
  fi
  if [[ -z "${RUN_STEP_COMMAND:-}" ]]; then
    echo "run-step: RUN_STEP_COMMAND must name the calling script, such as daily-prepare" >&2
    return 2
  fi
  shift
  local started=$SECONDS rc=0 elapsed
  echo "progress $RUN_STEP_COMMAND $name start elapsed=0.0s" >&2
  "$@" || rc=$?
  elapsed=$((SECONDS - started))
  if ((rc == 0)); then
    echo "progress $RUN_STEP_COMMAND $name end elapsed=${elapsed}.0s" >&2
  else
    echo "progress $RUN_STEP_COMMAND $name stop elapsed=${elapsed}.0s reason=exit code $rc" >&2
  fi
  return "$rc"
}
