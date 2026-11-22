#!/usr/bin/env bash
# Pick the TLC shards that a change reaches, for the plan job of .github/workflows/tla.yml.
#
# Usage: EVENT_NAME=pull_request BASE_SHA=<sha> bash scripts/tlc-plan.sh
#   --help  Print this usage and exit 0.
#
# Environment:
#   EVENT_NAME  The GitHub event: pull_request, push, or workflow_dispatch. Required.
#   BASE_SHA    The base commit of the pull request. Required for pull_request.
#   BEFORE_SHA  The commit before the push. Required for push.
#   GITHUB_OUTPUT        When set, the script appends "shards=<json>" to that file.
#   GITHUB_STEP_SUMMARY  When set, the script appends one line that says what it chose.
#
# Prints the JSON list of shard names on stdout, for example ["stall","main"]. The shards
# come from specs/shards.json through scripts/tlc_shards.py. A manual run, a push that has no
# usable base commit, and a diff that fails all select every shard, because a run that
# checks too much is safe and a run that checks nothing is not.
# Exit codes: 0 on success, 2 for a missing or unknown input.
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit 0
fi

event="${EVENT_NAME:-}"
if [[ -z "$event" ]]; then
  echo "tlc-plan: EVENT_NAME is not set; give pull_request, push, or workflow_dispatch" >&2
  exit 2
fi

shards() { python3 -I scripts/tlc_shards.py "$@"; }

# A SHA of forty zeros is what GitHub sends as the "before" of a new branch.
null_sha="0000000000000000000000000000000000000000"

chosen=""
reason=""
base=""
case "$event" in
workflow_dispatch) reason="a manual run" ;;
pull_request) base="${BASE_SHA:-}" ;;
push) base="${BEFORE_SHA:-}" ;;
*)
  echo "tlc-plan: unknown event '$event'; give pull_request, push, or workflow_dispatch" >&2
  exit 2
  ;;
esac

if [[ -z "$reason" ]]; then
  if [[ -z "$base" || "$base" == "$null_sha" ]]; then
    reason="no base commit to compare with"
  elif ! changed="$(git diff --name-only "$base" HEAD 2>/dev/null)"; then
    reason="the diff against $base failed"
  fi
fi

if [[ -n "$reason" ]]; then
  chosen="$(shards plan --all)"
  summary="TLC runs every shard: $reason."
else
  chosen="$(shards plan <<<"$changed")"
  if [[ "$chosen" == "[]" ]]; then
    summary="TLC runs no shard: no changed path is one that a shard covers."
  else
    summary="TLC runs these shards: $chosen."
  fi
fi

echo "$chosen"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "shards=$chosen" >>"$GITHUB_OUTPUT"
fi
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  echo "$summary" >>"$GITHUB_STEP_SUMMARY"
fi
echo "tlc-plan: $summary" >&2
