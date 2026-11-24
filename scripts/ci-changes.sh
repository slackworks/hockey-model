#!/usr/bin/env bash
# Decide whether a change needs the code jobs of CI (#170).
#
# Usage: git diff --name-only BASE HEAD | scripts/ci-changes.sh
#        scripts/ci-changes.sh --help
#
# stdin: the changed paths, one for each line.
# stdout: "code=true" or "code=false", ready to append to GITHUB_OUTPUT.
# stderr: the path that made the answer true, or the reason for an empty list.
# exit: 0 after an answer; 2 for a bad argument.
#
# A path is a docs path when it ends in ".md", at any depth. A docs path moves no code job,
# with one exception: a design note that a test reads is a code change, because that read
# compares the note with the code or the spec files. An empty list is a code change, because
# a diff that lists nothing cannot show that the change is docs only.
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
  echo "ci-changes.sh: unknown argument '$1'; run with --help" >&2
  exit 2
  ;;
esac

# Tests read these design notes, so a change to one is a code change.
read_by_tests=" docs/design/daily-prepare-tla.md docs/design/experiment-registry-tla.md "

seen=0
while IFS= read -r path || [[ -n "$path" ]]; do
  [[ -z "$path" ]] && continue
  seen=1
  if [[ "$path" != *.md || "$read_by_tests" == *" $path "* ]]; then
    echo "ci-changes.sh: $path is not a docs path, so the code jobs run" >&2
    echo "code=true"
    # The writer of the pipe gets SIGPIPE and fails the step when the input stays unread.
    cat >/dev/null
    exit 0
  fi
done

if ((seen == 0)); then
  echo "ci-changes.sh: the diff lists no path, so the code jobs run" >&2
  echo "code=true"
else
  echo "code=false"
fi
