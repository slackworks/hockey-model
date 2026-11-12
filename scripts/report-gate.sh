#!/usr/bin/env bash
# Decide whether this scheduled trigger publishes the daily report.
#
# Usage: report-gate.sh [--date YYYY-MM-DD] [--force] [--dry-run]
#                       [--margin-seconds N] [--help]
#   --date   Eastern game day to check. Default: the game day that holds now.
#   --force  Publish even outside the trigger slot. A day that is already
#            published is still skipped.
#   --margin-seconds  Seconds the build needs after this run starts. A published day is replaced
#            only when the run can finish before the first puck drop. Default: 0.
#   --dry-run  Build a report that the caller does not publish. This implies --force,
#            and the published-release check is skipped, so a published day is due again.
#
# Prints one JSON object on stdout:
#   {"due": bool, "publish": bool, "tag": str, "reason": str, "date": str}.
# "due" says the report is built. "publish" says the report becomes a release, and it is
# false for --dry-run. "reason" is one of:
#   due                 the day has no release and this run beats the puck drop
#   late                the day has no release and this run does not beat the puck drop; the
#                       first report of the day still publishes
#   replace             the day has a release and this run beats the puck drop; it is replaced
#   already-published   the day has a release and this run does not replace it
#   no-games            the schedule lists no games, so nothing publishes
#   dry-run             the report is built and not published
# Exits non-zero when the schedule payload is malformed, the release lookup fails, or the
# decision step fails or prints a payload that lacks a field.
set -euo pipefail

day=""
force=false
dry_run=false
margin_s=0

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

while (($# > 0)); do
  case "$1" in
  --date)
    if (($# < 2)); then
      echo "report-gate: --date needs a value YYYY-MM-DD; see --help" >&2
      exit 2
    fi
    day="$2"
    shift 2
    ;;
  --margin-seconds)
    if (($# < 2)) || [[ ! "$2" =~ ^[0-9]+$ ]]; then
      echo "report-gate: --margin-seconds needs whole seconds; see --help" >&2
      exit 2
    fi
    margin_s="$2"
    shift 2
    ;;
  --force)
    force=true
    shift
    ;;
  --dry-run)
    dry_run=true
    force=true
    shift
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "report-gate: unknown argument '$1'; see --help" >&2
    exit 2
    ;;
  esac
done

# shellcheck source=scripts/valid-day.sh
source "$(dirname "$0")/valid-day.sh"
if [[ -n "$day" ]] && ! is_valid_day "$day"; then
  echo "report-gate: --date '$day' is not a real date YYYY-MM-DD; see --help" >&2
  exit 2
fi

# The schedule read is one HTTP request. Each run then makes one release lookup, so a
# run after the puck drop is still answered from the release state.
# Without --date, report-due picks the Eastern game day that holds now.
args=()
if [[ -n "$day" ]]; then
  args+=(--date "$day")
fi
if [[ "$force" == true ]]; then
  args+=(--force)
fi
args+=(--margin-seconds "$margin_s")
status_json="$(uv run hockey-model report-due ${args[@]+"${args[@]}"})" || {
  echo "report-gate: report-due failed; its error is above" >&2
  exit 1
}
required='(.due | type == "boolean") and (.tag | type == "string")'
required+=' and (.games | type == "number") and (.date | type == "string")'
if ! jq -e "$required" <<<"$status_json" >/dev/null; then
  echo "report-gate: report-due payload lacks due, tag, games, or date: $status_json" >&2
  exit 1
fi
tag="$(jq -r '.tag' <<<"$status_json")"
due="$(jq -r '.due' <<<"$status_json")"
games="$(jq -r '.games' <<<"$status_json")"
day="$(jq -r '.date' <<<"$status_json")"
# Optional: only the late-run warning names it.
first_drop="$(jq -r '.first_puck_drop_utc // ""' <<<"$status_json")"

# The decision is in report_gate.py. This script reads the facts: the schedule above, and the
# release below. A dry run and a day with no games never reach the release lookup.
decide() {
  uv run hockey-model report-decide --due "$due" --games "$games" --date "$day" \
    --first-puck-drop "$first_drop" --margin-seconds "$margin_s" --published "$1" \
    ${force_flag[@]+"${force_flag[@]}"} ${dry_flag[@]+"${dry_flag[@]}"}
}

force_flag=()
dry_flag=()
[[ "$force" == true ]] && force_flag=(--force)
[[ "$dry_run" == true ]] && dry_flag=(--dry-run)

decided() {
  local out
  out="$(decide "$1")" || {
    echo "report-gate: report-decide failed; its error is above" >&2
    return 1
  }
  local fields='(.due | type == "boolean") and (.publish | type == "boolean")'
  fields+=' and (.reason | type == "string") and (.lookup_needed | type == "boolean")'
  if ! jq -e "$fields" <<<"$out" >/dev/null 2>&1; then
    echo "report-gate: report-decide payload lacks due, publish, reason, or lookup_needed: $out" >&2
    return 1
  fi
  printf '%s' "$out"
}

decision="$(decided unknown)" || exit 1
if [[ "$(jq -r '.lookup_needed' <<<"$decision")" == "true" ]]; then
  # Ask for the one tag. "release not found" means the day is not published yet; any
  # other failure (auth, network) stops the run. check-daily-report.sh has the same
  # lookup, so change both together.
  view_err="$(gh release view "$tag" 2>&1 >/dev/null)" && published=true || published=false
  if [[ "$published" == false && "$view_err" != *"release not found"* ]]; then
    echo "report-gate: gh release view $tag failed: $view_err" >&2
    exit 1
  fi
  decision="$(decided "$published")" || exit 1
fi
warning="$(jq -r '.warning // ""' <<<"$decision")"
if [[ -n "$warning" ]]; then
  echo "$warning" >&2
fi
jq -c --arg tag "$tag" --arg date "$day" '{due, publish, tag: $tag, reason, date: $date}' \
  <<<"$decision"
