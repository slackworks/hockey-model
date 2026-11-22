#!/usr/bin/env bash
# Decide the result of the CI code jobs, with one check that a branch rule can require (#170).
#
# Usage: CHANGES_RESULT=success CODE=true VALIDATION_RESULT=success TEST_RESULT=success \
#          scripts/ci-gate.sh
#        scripts/ci-gate.sh --help
#
# CHANGES_RESULT, VALIDATION_RESULT, and TEST_RESULT are the results that GitHub gives a job:
# success, failure, cancelled, or skipped. CODE is the output of the changes job: true or false.
# A changes job that failed has no output, so CODE may be empty then.
#
# A job that an `if` skips counts as passed for a required check. So a failed changes job would
# let validation and test skip with no failure that blocks the merge. This script closes that
# gap: the changes job must succeed. With CODE=true, validation and test must succeed. With
# CODE=false, both must be skipped.
#
# stdout: nothing. stderr: one line for each failed rule.
# Exit codes: 0 when every rule holds, 1 when a rule fails, 2 for a bad argument or a missing
# variable.
set -euo pipefail

# shellcheck source=scripts/usage.sh
source "$(dirname "$0")/usage.sh"

case "${1:-}" in
-h | --help)
  usage
  exit 0
  ;;
"") ;;
*)
  echo "ci-gate.sh: unknown argument '$1'; run with --help" >&2
  exit 2
  ;;
esac

for name in CHANGES_RESULT VALIDATION_RESULT TEST_RESULT; do
  if [[ -z "${!name:-}" ]]; then
    echo "ci-gate.sh: $name is not set; the workflow passes it from needs" >&2
    exit 2
  fi
done

if [[ "$CHANGES_RESULT" != "success" ]]; then
  echo "ci-gate.sh: the changes job result is '$CHANGES_RESULT', so no job shows that the code is fine" >&2
  exit 1
fi

case "${CODE:-}" in
true) expected="success" ;;
false) expected="skipped" ;;
*)
  echo "ci-gate.sh: CODE is '${CODE:-}', and the changes job sets true or false" >&2
  exit 1
  ;;
esac

failed=0
check() {
  local label="$1" result="$2"
  if [[ "$result" != "$expected" ]]; then
    echo "ci-gate.sh: the $label job result is '$result', and CODE=$CODE needs '$expected'" >&2
    failed=1
  fi
}
check validation "$VALIDATION_RESULT"
check test "$TEST_RESULT"
exit "$failed"
