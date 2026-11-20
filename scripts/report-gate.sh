#!/usr/bin/env bash
# Decide whether this scheduled trigger publishes the daily report.
#
# Usage: report-gate.sh [--date YYYY-MM-DD] [--force] [--dry-run]
#                       [--margin-seconds N] [--prep-started ISO] [--help]
#   --date   Eastern game day to check. Default: the game day that holds now.
#   --force  Publish even outside the trigger slot. A day that is already
#            published is still skipped.
#   --margin-seconds  Seconds the build needs after this run starts. A published day is replaced
#            only when the run can finish before the first puck drop. Default: 0.
#   --prep-started ISO
#            Start of the daily-prepare run that holds the store group now, or empty when no
#            run holds it. The report queues behind that run, and GitHub counts the prepare
#            timeout from its start, so a late prepare can hold the group past the puck drop.
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
#   prepare-wait        a prepare run holds the store group and can run past the puck drop, so
#                       this run would publish after it; the run is skipped and not built
# Exits non-zero when the schedule payload is malformed, the release lookup fails, the
# decision step fails or prints a payload that lacks a field, or the day has no game inside
# its window while the feed lists a game outside it.
set -euo pipefail

day=""
have_date=false
force=false
dry_run=false
prep_started=""
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
    have_date=true
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
  --prep-started)
    if (($# < 2)); then
      echo "report-gate: --prep-started needs an ISO instant; see --help" >&2
      exit 2
    fi
    prep_started="$2"
    shift 2
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
# shellcheck source=scripts/release-exists.sh
source "$(dirname "$0")/release-exists.sh"
if [[ "$have_date" == true ]] && ! is_valid_day "$day"; then
  echo "report-gate: --date '$day' is not a real date YYYY-MM-DD; see --help" >&2
  exit 2
fi

# The schedule read is one HTTP request. Each run then makes one release lookup, so a
# run after the puck drop is still answered from the release state.
# Without --date, report-due picks the Eastern game day that holds now.
args=()
if [[ "$have_date" == true ]]; then
  args+=(--date "$day")
fi
if [[ "$force" == true ]]; then
  args+=(--force)
fi
args+=(--margin-seconds "$margin_s")
args+=(--prep-started "$prep_started")
status_json="$(uv run hockey-model report-due ${args[@]+"${args[@]}"})" || {
  echo "report-gate: report-due failed; its error is above" >&2
  exit 1
}
required='(.due | type == "boolean") and (.tag | type == "string")'
required+=' and (.games | type == "number") and (.date | type == "string")'
required+=' and (.games_outside_window | type == "number")'
required+=' and (.prep_too_early | type == "boolean")'
required+=' and (.prep_blocks_report | type == "boolean")'
if ! jq -e "$required" <<<"$status_json" >/dev/null; then
  echo "report-gate: report-due payload lacks due, tag, games, date, games_outside_window," \
    "prep_too_early, or prep_blocks_report: $status_json" >&2
  exit 1
fi
tag="$(jq -r '.tag' <<<"$status_json")"
due="$(jq -r '.due' <<<"$status_json")"
games="$(jq -r '.games' <<<"$status_json")"
day="$(jq -r '.date' <<<"$status_json")"
outside="$(jq -r '.games_outside_window' <<<"$status_json")"
# A feed that lists the day under the wrong date drops every game, and the gate must not
# read that as an off day. check-daily-report.sh refuses the same state.
if [[ "$games" == "0" && "$outside" != "0" ]]; then
  echo "report-gate: $day has no game inside its window, and the feed lists $outside" \
    "game(s) outside it. The feed date is probably wrong; check report-due's stderr for" \
    "the game ids." >&2
  exit 1
fi
# A game outside the window is a dropped row even when other games remain, so the report
# would publish a partial day. Say so, and let the operator decide whether to force it.
if [[ "$outside" != "0" ]]; then
  echo "report-gate: warning: $day drops $outside feed game(s) that fall outside its" \
    "window; see report-due's stderr for the game ids" >&2
fi
# Optional: only the late-run warning names it.
first_drop="$(jq -r '.first_puck_drop_utc // ""' <<<"$status_json")"
blocks="$(jq -r '.prep_blocks_report' <<<"$status_json")"
# A due run would queue behind that prepare and publish after the puck drop. The gate holds no
# store lock, so it answers here: this run builds nothing, and the next trigger runs again.
# A forced run is the operator's call, and it still queues, so it is not refused here.
if [[ "$blocks" == "true" && "$due" == "true" && "$force" != true ]]; then
  echo "report-gate: warning: a daily-prepare run holds the hockey-model-store group, and its" \
    "timeout reaches past the first puck drop of $day. This run would wait behind it and" \
    "publish after the puck drop, so it builds nothing (#163)." >&2
  jq -c --arg tag "$tag" --arg date "$day" \
    '{due: false, publish: false, tag: $tag, reason: "prepare-wait", date: $date}' <<<"{}"
  exit 0
fi

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
  # Ask for the one tag. A 404 means the day is not published yet; any other answer (auth,
  # network) stops the run. check-daily-report.sh has the same lookup, so change both together.
  answer="$(release_status "$tag")"
  case "$answer" in
  published) published=true ;;
  missing) published=false ;;
  *)
    echo "report-gate: the release lookup for $tag failed: ${answer#failed: }" >&2
    exit 1
    ;;
  esac
  decision="$(decided "$published")" || exit 1
fi
warning="$(jq -r '.warning // ""' <<<"$decision")"
if [[ -n "$warning" ]]; then
  echo "$warning" >&2
fi
jq -c --arg tag "$tag" --arg date "$day" '{due, publish, tag: $tag, reason, date: $date}' \
  <<<"$decision"
