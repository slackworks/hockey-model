#!/usr/bin/env bash
# Publish the daily report as a GitHub release tagged reports-DATE.
#
# Usage: publish-daily-report.sh --date YYYY-MM-DD --report-dir DIR [--help]
#   --date        Eastern game day of the report.
#   --report-dir  Directory with report-DATE.md, report-DATE.json, backtest-DATE.json,
#                 and backtest-games-DATE.csv.
#
# The release target is GITHUB_SHA, the commit that built the first report of the day. A later
# build for the same day replaces the release files and notes, so the release can change until
# the first puck drop. The notes say so.
set -euo pipefail

day=""
report_dir=""

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

while (($# > 0)); do
  case "$1" in
  --date)
    if (($# < 2)); then
      echo "publish-daily-report: --date needs a value YYYY-MM-DD; see --help" >&2
      exit 2
    fi
    day="$2"
    shift 2
    ;;
  --report-dir)
    if (($# < 2)); then
      echo "publish-daily-report: --report-dir needs a directory; see --help" >&2
      exit 2
    fi
    report_dir="$2"
    shift 2
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "publish-daily-report: unknown argument '$1'; see --help" >&2
    exit 2
    ;;
  esac
done

if [[ -z "$day" || -z "$report_dir" ]]; then
  echo "publish-daily-report: --date and --report-dir are required; see --help" >&2
  exit 2
fi
# shellcheck source=scripts/valid-day.sh
source "$(dirname "$0")/valid-day.sh"
if ! is_valid_day "$day"; then
  echo "publish-daily-report: --date '$day' is not a real date YYYY-MM-DD" >&2
  exit 2
fi
if [[ -z "${GITHUB_SHA:-}" ]]; then
  echo "publish-daily-report: GITHUB_SHA must name the commit that built the report" >&2
  exit 2
fi

md="$report_dir/report-$day.md"
json="$report_dir/report-$day.json"
backtest="$report_dir/backtest-$day.json"
backtest_games="$report_dir/backtest-games-$day.csv"
for file in "$md" "$json" "$backtest" "$backtest_games"; do
  if [[ ! -s "$file" ]]; then
    echo "publish-daily-report: $file is missing or empty; the report was not built" >&2
    exit 1
  fi
done
for file in "$json" "$backtest"; do
  if ! jq -e . "$file" >/dev/null; then
    echo "publish-daily-report: $file is not valid JSON; the report was not published" >&2
    exit 1
  fi
done
if [[ "$(head -n 1 "$backtest_games")" != *,* ]]; then
  echo "publish-daily-report: $backtest_games has no CSV header; the backtest was not built" >&2
  exit 1
fi

tag="reports-$day"
# The notes say the report can change until the first puck drop, and they name this build.
notes="$report_dir/release-notes-$day.md"
{
  echo "This report can be replaced by a later build until the first puck drop of the game day."
  echo "This build: commit $GITHUB_SHA, finished $(date -u +%Y-%m-%dT%H:%M:%SZ) UTC."
  echo
  cat "$md"
} >"$notes"
assets=("$md" "$json" "$backtest" "$backtest_games")
if view_err="$(gh release view "$tag" 2>&1 >/dev/null)"; then
  # The release exists, so this build replaces its files and notes. The target commit of the
  # release stays the one that created it.
  gh release upload "$tag" "${assets[@]}" --clobber
  gh release edit "$tag" --notes-file "$notes"
elif [[ "$view_err" == *"release not found"* ]]; then
  gh release create "$tag" "${assets[@]}" \
    --target "$GITHUB_SHA" \
    --title "NHL predictions $day" \
    --notes-file "$notes"
else
  echo "publish-daily-report: gh release view $tag failed: $view_err" >&2
  exit 1
fi
