#!/usr/bin/env bash
# Ask whether a release tag exists, with the HTTP status rather than the message text.
#
# Usage: source release-exists.sh; release_status TAG
#   Prints one of: "published", "missing", or "failed: DETAIL".
#   "missing" means the repository answered, and the release path answered 404. "failed" covers
#   every other answer, such as an auth or network error, so a caller never reads a broken
#   lookup as a missing release.
#
# The status is read from the response, because a message-text match breaks whenever gh changes
# its wording. `gh api --include` prints the status line, and the body is the response JSON.
#
# GitHub answers 404 for a repository that the token cannot see, as well as for an absent
# release. The repository is read first, so a token that cannot see the repository returns
# "failed" and not "missing", which would read as an unpublished day.

# Print the status code of an `gh api --include` response, or nothing when it has none.
_http_status() {
  local status
  status="$(sed -n 's|^HTTP/[0-9.]* \([0-9]\{3\}\).*|\1|p' <<<"$1")"
  # head would close the pipe early, and pipefail would then read a SIGPIPE as a failure.
  printf '%s' "${status%%$'\n'*}"
}

release_status() {
  local tag="$1"
  local out status
  if ! out="$(gh api --include "repos/{owner}/{repo}" 2>&1)"; then
    status="$(_http_status "$out")"
    if [[ "$status" != "404" ]]; then
      printf 'failed: cannot read the repository: %s' "$out"
      return 0
    fi
    printf 'failed: the token cannot see this repository, so a missing release cannot be' \
      "told apart from a missing repository: %s" "$out"
    return 0
  fi
  if ! out="$(gh api --include "repos/{owner}/{repo}/releases/tags/${tag}" 2>&1)"; then
    status="$(_http_status "$out")"
    if [[ "$status" == "404" ]]; then
      printf 'missing'
      return 0
    fi
    printf 'failed: %s' "$out"
    return 0
  fi
  printf 'published'
}
