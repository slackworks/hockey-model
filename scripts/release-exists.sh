#!/usr/bin/env bash
# Ask whether a release tag exists, with the HTTP status rather than the message text.
#
# Usage: source release-exists.sh; release_status TAG
#   Prints one of: "published", "missing", or "failed: DETAIL".
#   "missing" means the API answered 404, which is the only answer that means the release does
#   not exist. "failed" covers every other answer, such as an auth or network error, so a caller
#   never reads a broken lookup as a missing release.
#
# The status is read from the response, because a message-text match breaks whenever gh changes
# its wording. `gh api --include` prints the status line, and the body is the response JSON.

release_status() {
  local tag="$1"
  local out status
  if ! out="$(gh api --include "repos/{owner}/{repo}/releases/tags/${tag}" 2>&1)"; then
    status="$(sed -n 's|^HTTP/[0-9.]* \([0-9]\{3\}\).*|\1|p' <<<"$out")"
    # head would close the pipe early, and pipefail would then read a SIGPIPE as a failure.
    status="${status%%$'\n'*}"
    if [[ "$status" == "404" ]]; then
      printf 'missing'
      return 0
    fi
    printf 'failed: %s' "$out"
    return 0
  fi
  printf 'published'
}
