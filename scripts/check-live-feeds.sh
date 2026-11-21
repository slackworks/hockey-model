#!/usr/bin/env bash
# Alert when a live feed has stayed a known gap for longer than the limit.
#
# Usage: check-live-feeds.sh [--help]
#
# Reads `uv run hockey-model store gaps --live`, which names each live feed that is a known
# gap and says whether its gap is older than the limit (LIVE_FEED_MAX_AGE in the package).
# It also fails for a feed whose newest read is older than the limit, or that has no read.
# The store file comes from HOCKEY_MODEL_DB.
#
# Exits 0 when no live feed gap is over the limit. Exits 1 with one ::error:: line for each
# gap that is over it, and with one line when the command fails or prints another shape.
# Exits 2 on bad arguments.
set -euo pipefail

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
"") ;;
-h | --help)
  usage
  exit 0
  ;;
*)
  echo "check-live-feeds: unknown argument '$1'; see --help" >&2
  exit 2
  ;;
esac

if ! command -v jq >/dev/null 2>&1; then
  echo "check-live-feeds: jq is not on PATH; install jq" >&2
  exit 1
fi

report="$(uv run hockey-model store gaps --live)" || {
  echo "check-live-feeds: 'store gaps --live' failed; its error is above" >&2
  exit 1
}
if ! jq -e '(.limit_hours | type == "number") and (.gaps | type == "array")
  and all(.gaps[]; (.source | type == "string") and (.url | type == "string")
    and (.age_hours | type == "number") and (.over_limit | type == "boolean"))
  and (.feeds | type == "array")
  and all(.feeds[]; (.source | type == "string") and (.over_limit | type == "boolean"))' \
  <<<"$report" >/dev/null; then
  echo "check-live-feeds: the gaps payload has another shape than the schema says:" \
    "$report" >&2
  exit 1
fi

# Arithmetic on a number prints it the same in each jq version: 24, not 24.0.
late="$(jq -r '.limit_hours as $limit | .gaps[] | select(.over_limit)
  | "::error::check-live-feeds: \(.source) has failed since \(.gap_since), \(.age_hours * 1) hours, "
    + "over the limit of \($limit * 1) hours: \(.url). Check that the host is reachable and does not "
    + "block this runner. The next good read clears the gap by itself."' <<<"$report")"
silent="$(jq -r '.limit_hours as $limit | .feeds[] | select(.over_limit)
  | "::error::check-live-feeds: \(.source) has no read in the last \($limit * 1) hours "
    + "(newest read: \(.newest_read // "none")). Check that daily-prepare and daily-report "
    + "still run and save the store cache."' <<<"$report")"
if [[ -n "$late" || -n "$silent" ]]; then
  [[ -z "$late" ]] || echo "$late" >&2
  [[ -z "$silent" ]] || echo "$silent" >&2
  exit 1
fi
echo "ok: no live feed has been a gap for longer than the limit, and each feed has a recent read"
