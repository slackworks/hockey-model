#!/usr/bin/env bash
# Fail unless the CI jobs of a commit ended in success (#299).
#
# Usage: check-commit-ci.sh --sha SHA
#        check-commit-ci.sh --help
#
# The release workflow runs this on the commit that a tag names, so a tag on a commit with a
# red or missing CI run publishes nothing. The jobs are in REQUIRED_JOBS below: ci-result stands
# for validation and test, and it passes for a Markdown-only change, which skips them.
# A job that ran more than once is judged by its newest run.
#
# The command needs the gh CLI and jq, and a token that can read check runs of the repository.
#
# stdout: nothing. stderr: one line for each job that did not end in success.
# Exit codes: 0 when every required job succeeded, 1 when a job did not, or when the lookup
# failed, 2 for a bad argument.
set -euo pipefail

REQUIRED_JOBS=(ci-result editorconfig smoke zizmor)

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

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

runs="$(gh api "repos/{owner}/{repo}/commits/$sha/check-runs?per_page=100")" ||
  fail "cannot read the check runs of $sha; the token needs checks: read"
jq -e '.check_runs | type == "array"' >/dev/null <<<"$runs" ||
  fail "the answer for $sha holds no check_runs list"

if [[ "$(jq '.check_runs | length' <<<"$runs")" == "0" ]]; then
  fail "no check run exists for $sha; wait for the ci workflow, or push the commit to main first"
fi

failed=0
for job in "${REQUIRED_JOBS[@]}"; do
  conclusion="$(jq -r --arg name "$job" '
    [.check_runs[] | select(.name == $name)] | sort_by(.started_at) | last
    | if . == null then "missing" else (.conclusion // .status) end' <<<"$runs")"
  if [[ "$conclusion" != "success" ]]; then
    echo "check-commit-ci: the ci job '$job' is '$conclusion' on $sha, and a release needs 'success'" >&2
    failed=1
  fi
done
exit "$failed"
