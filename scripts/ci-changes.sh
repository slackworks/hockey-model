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
# with one exception: tests/test_tla_appendix.py compares the appendix of that design note
# with the spec files, so a change to the note is a code change. An empty list is a code
# change, because a diff that lists nothing cannot show that the change is docs only.
set -euo pipefail

# Paths under .md that a test reads. A change to one of them runs the tests.
TESTED_DOCS=("docs/design/daily-prepare-tla.md")

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

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

is_docs() {
  local path="$1" tested
  for tested in "${TESTED_DOCS[@]}"; do
    [[ "$path" == "$tested" ]] && return 1
  done
  [[ "$path" == *.md ]]
}

seen=0
while IFS= read -r path || [[ -n "$path" ]]; do
  [[ -z "$path" ]] && continue
  seen=1
  if ! is_docs "$path"; then
    echo "ci-changes.sh: $path is not a docs path, so the code jobs run" >&2
    echo "code=true"
    exit 0
  fi
done

if ((seen == 0)); then
  echo "ci-changes.sh: the diff lists no path, so the code jobs run" >&2
  echo "code=true"
else
  echo "code=false"
fi
