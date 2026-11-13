#!/usr/bin/env bash
# Publish the daily report as a GitHub release tagged reports-DATE.
#
# Usage: publish-daily-report.sh --date YYYY-MM-DD --report-dir DIR [--help]
#   --date        Eastern game day of the report.
#   --report-dir  Directory with report-DATE.md, report-DATE.json, backtest-DATE.json,
#                 and backtest-games-DATE.csv.
#
# The release target is GITHUB_SHA, the commit that built the first report of the day. Each build
# adds its report as report-DATE-HHMMSSZ files, named by its generated_at time. A published
# version is never replaced. The report-DATE files and the backtest files move to the newest build.
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
# generated_at comes from report_json as ISO 8601 UTC with microseconds. The regex proves the
# shape, so the fixed columns of HHMMSS are safe to read.
generated="$(jq -r '.generated_at // empty' "$json")"
if [[ ! "$generated" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00$ ]]; then
  echo "publish-daily-report: $json has no generated_at as ISO 8601 UTC; the report was not published" >&2
  exit 1
fi
stamp="${generated:11:2}${generated:14:2}${generated:17:2}Z"
version_md="$report_dir/report-$day-$stamp.md"
version_json="$report_dir/report-$day-$stamp.json"
cp "$md" "$version_md"
cp "$json" "$version_json"
latest=("$md" "$json" "$backtest" "$backtest_games")
notes="$report_dir/release-notes-$day.md"
{
  echo "Every build's report is kept. Each report-$day-HHMMSSZ file set is one build, named by its"
  echo "generated_at time (UTC). The report-$day files and the backtest files are the newest build."
  echo "This build: commit $GITHUB_SHA, finished $(date -u +%Y-%m-%dT%H:%M:%SZ) UTC, report-$day-$stamp."
  echo
  cat "$md"
} >"$notes"

# A version name is its build's generated_at, so the same name is the same build. A retry of
# that build finds its bytes already on the release and skips the upload. Other bytes under the
# same name are a collision, and the script stops, because a published version is never replaced.
publish_version() {
  local file="$1" name
  name="$(basename "$file")"
  if ! grep -qxF "$name" <<<"$existing_assets"; then
    gh release upload "$tag" "$file"
    return 0
  fi
  gh release download "$tag" --pattern "$name" --dir "$check_dir"
  if cmp -s "$file" "$check_dir/$name"; then
    echo "publish-daily-report: $name is already on $tag with the same bytes; skipped." >&2
    return 0
  fi
  echo "publish-daily-report: $name is already on $tag with other bytes; a version is never replaced. Two builds share one second. Find the second build and publish it again." >&2
  exit 1
}

if view_err="$(gh release view "$tag" 2>&1 >/dev/null)"; then
  # The release exists. The alias and the backtest files move to this build with --clobber.
  existing_assets="$(gh release view "$tag" --json assets --jq '.assets[].name')"
  check_dir="$report_dir/published-check"
  mkdir -p "$check_dir"
  publish_version "$version_md"
  publish_version "$version_json"
  gh release upload "$tag" "${latest[@]}" --clobber
  gh release edit "$tag" --notes-file "$notes"
elif [[ "$view_err" == *"release not found"* ]]; then
  gh release create "$tag" "$version_md" "$version_json" "${latest[@]}" \
    --target "$GITHUB_SHA" \
    --title "NHL predictions $day" \
    --notes-file "$notes"
else
  echo "publish-daily-report: gh release view $tag failed: $view_err" >&2
  exit 1
fi
