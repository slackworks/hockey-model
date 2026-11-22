#!/usr/bin/env bash
# Fail unless the ci workflow succeeded for a push of a commit (#299).
#
# Usage: check-commit-ci.sh --sha SHA
#        check-commit-ci.sh --help
#
# The release workflow runs this on the commit that a tag names, so a tag on a commit with a red
# or a missing ci run publishes nothing. The run must be a push run of ci.yml, which exists for a
# commit that reached main. A pull request run of the same commit does not count, because a
# Markdown-only pull request skips the validation and test jobs. The conclusion of a workflow
# run is success only when every job succeeded or was skipped, and the ci-result job fails when
# a skip is wrong. A commit with more than one push run is judged by its newest run.
#
# The command needs the gh CLI and jq, and a token that can read workflow runs.
#
# stdout: nothing. stderr: the reason for a failure.
# Exit codes: 0 when the newest push run succeeded, 1 when it did not, or when the lookup
# failed, 2 for a bad argument.
set -euo pipefail

WORKFLOW="ci.yml"

# shellcheck source=scripts/usage.sh
source "$(dirname "$0")/usage.sh"

fail() {
  echo "check-commit-ci: $1" >&2
  exit "${2:-1}"
}

sha=""
while (($# > 0)); do
  case "$1" in
  --sha)
    sha="${2:-}"
    shift "$(($# >= 2 ? 2 : 1))"
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *) fail "unknown argument '$1'; run with --help" 2 ;;
  esac
done

# Shape: a full commit ID. The API treats a shorter text as a branch or a tag name.
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail "--sha '$sha' is not a 40-character commit ID" 2

query="head_sha=$sha&event=push&per_page=100"
runs="$(gh api "repos/{owner}/{repo}/actions/workflows/$WORKFLOW/runs?$query")" ||
  fail "cannot read the $WORKFLOW runs of $sha; the token needs actions: read"
jq -e '.workflow_runs | type == "array"' >/dev/null <<<"$runs" ||
  fail "the answer for $sha holds no workflow_runs list"

newest="$(jq -r '.workflow_runs | sort_by(.created_at) | last | if . == null then "none"
  else (.conclusion // .status) end' <<<"$runs")"
case "$newest" in
success) ;;
none)
  fail "no push run of $WORKFLOW exists for $sha; tag a commit that reached main, then wait"
  ;;
*)
  fail "the newest push run of $WORKFLOW for $sha is '$newest', and a release needs 'success'"
  ;;
esac
