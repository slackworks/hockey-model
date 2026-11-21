#!/usr/bin/env bash
# Run one step of a script and log its progress lines to stderr, in the format of
# src/hockey_model/progress.py. Source this file. It is not a command.
#
# Usage: RUN_STEP_COMMAND=NAME run_step STEP CMD [ARG...]
#   RUN_STEP_COMMAND  The name of the calling script, such as daily-prepare.
#   STEP              One word that names the step, such as backtest. A caller passes a literal.
#
# The function logs "progress NAME STEP start elapsed=0.0s", runs the command, and then logs
# "progress NAME STEP end elapsed=Ns", or "progress NAME STEP stop elapsed=Ns reason=exit code N"
# when the command failed. The command keeps its own stdout and stderr. The function returns
# the exit code of the command, so a caller under "set -e" stops with that code.

run_step() {
  local command="${RUN_STEP_COMMAND:?set RUN_STEP_COMMAND to the name of the calling script}"
  local name="$1"
  shift
  local started=$SECONDS rc=0 elapsed
  echo "progress $command $name start elapsed=0.0s" >&2
  "$@" || rc=$?
  elapsed=$((SECONDS - started))
  if ((rc == 0)); then
    echo "progress $command $name end elapsed=${elapsed}.0s" >&2
  else
    echo "progress $command $name stop elapsed=${elapsed}.0s reason=exit code $rc" >&2
  fi
  return "$rc"
}
