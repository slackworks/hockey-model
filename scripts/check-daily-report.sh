#!/usr/bin/env bash
# Alert when a game day has no daily report, or when its report came after every puck drop.
#
# Usage: check-daily-report.sh --date YYYY-MM-DD [--help]
#   --date  Eastern game day to check. Run this after the last trigger of that day.
#
# Exits 0 when the day needs no alert: it has no games, or its report has a pre-start row.
# Exits 1 when the day has games and no release, or when every game row of its report was
# made after the start. The alert is one ::error:: line on stderr.
# Exits 2 on bad arguments. Exits 1 when a GitHub or report-due call fails.
set -euo pipefail

day=""
work_dir=".tmp/check-daily-report"

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

while (($# > 0)); do
  case "$1" in
  --date)
    if (($# < 2)); then
      echo "check-daily-report: --date needs a value YYYY-MM-DD; see --help" >&2
      exit 2
    fi
    day="$2"
    shift 2
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "check-daily-report: unknown argument '$1'; see --help" >&2
    exit 2
    ;;
  esac
done

if [[ ! "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "check-daily-report: --date must be YYYY-MM-DD, got '$day'; see --help" >&2
  exit 2
fi

status_json="$(uv run hockey-model report-due --date "$day")" || {
  echo "check-daily-report: report-due failed for $day; its error is above" >&2
  exit 1
}
if ! jq -e '(.games | type == "number") and (.tag | type == "string")' <<<"$status_json" \
  >/dev/null; then
  echo "check-daily-report: report-due payload lacks games or tag: $status_json" >&2
  exit 1
fi
games="$(jq -r '.games' <<<"$status_json")"
tag="$(jq -r '.tag' <<<"$status_json")"
if [[ "$games" == "0" ]]; then
  echo "ok $day: the schedule lists no games, so no report is expected"
  exit 0
fi

# "release not found" means no report was published. Any other failure stops the check, so
# an auth or network error is not read as a missing report. report-gate.sh has the same
# lookup, so change both together.
view_err="$(gh release view "$tag" 2>&1 >/dev/null)" && published=true || published=false
if [[ "$published" == false ]]; then
  if [[ "$view_err" != *"release not found"* ]]; then
    echo "check-daily-report: gh release view $tag failed: $view_err" >&2
    exit 1
  fi
  echo "::error::check-daily-report: $day has $games game(s) and no report release ($tag)." >&2
  exit 1
fi

mkdir -p "$work_dir"
report="$work_dir/report-$day.json"
if ! dl_err="$(gh release download "$tag" --pattern "report-$day.json" --output "$report" \
  --clobber 2>&1)"; then
  echo "check-daily-report: could not download report-$day.json from $tag: $dl_err" >&2
  exit 1
fi
if ! jq empty "$report" >/dev/null 2>&1; then
  echo "check-daily-report: $report is not valid JSON; the check cannot judge the report" >&2
  exit 1
fi
if ! jq -e 'type == "object"' "$report" >/dev/null; then
  echo "check-daily-report: $report is not a JSON object; the check cannot judge the report" >&2
  exit 1
fi
# A report whose rows lack a boolean made_after_start has changed shape. Judging it would
# pass a broken report as healthy, so the check fails and names the path.
if ! jq -e '(.games | type == "array") and all(.games[]; .made_after_start | type == "boolean")' \
  "$report" >/dev/null; then
  echo "check-daily-report: $report lacks a boolean .games[].made_after_start; the report shape changed" >&2
  exit 1
fi

# A report with no rows has no game to judge, so it raises no alert. A postponed row has no
# puck drop and no prediction, so it must not hide a report whose other rows came late.
if jq -e '[.games[] | select(.state != "PPD")] as $played
  | ($played | length) > 0 and all($played[]; .made_after_start == true)' "$report" \
  >/dev/null; then
  echo "::error::check-daily-report: every game row of $tag was made after its start." \
    "The report came after the puck drop." >&2
  exit 1
fi
echo "ok $day: report $tag has at least one pre-start row"
