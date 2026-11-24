#!/usr/bin/env bash
# Write the daily report for one game day: refresh the starters, then predict.
#
# Usage: daily-report.sh --date YYYY-MM-DD --out-dir DIR [--skip-unchanged] [--help]
#   --date            Eastern game day to predict.
#   --out-dir         Directory for the report and backtest files. Created when missing.
#   --skip-unchanged  After the prediction, read the release of the day. When it is complete
#                     for its newest build (the index, report-DATE.json, and the notes all
#                     name that build) and the inputs_sha256 of that build equals the one of
#                     this build, exit 0 and write no complete marker, so nothing publishes.
#                     A day with no release, a release that is behind its newest build, and
#                     a published report with no inputs_sha256 build as usual. A failed
#                     release call stops the run with exit code 1. This script reads
#                     REPORT_GH_TOKEN once, then unsets it, so only the release calls of the
#                     compare step carry it. No other command sees it.
#
# Reads the store path from HOCKEY_MODEL_DB. The daily-prepare run does the slow work each
# hour. When its prepared-DATE marker is missing, this script skips with exit code 0 while the
# first puck drop of the day is still ahead, because the next hourly prepare can still make the
# marker. After the first puck drop the same missing marker stops the script with exit code 1
# and writes no report. Run the daily-prepare workflow for the day, then run this script
# again. Any failed step stops the script with its exit code, so a failed run writes no report.
#
# Reads REPORT_BUDGET_S, a whole number of seconds. The default is set in the budget line below.
# Each step logs its seconds to stderr. The clock has one-second resolution, so a run can pass
# the budget by less than one second and still pass this check.
# When the whole run passes the budget, the script exits 1 after it writes the report. The
# workflow still uploads and publishes that report, and the build job stays failed.
set -euo pipefail

day=""
out_dir=""
skip_unchanged=0
# The release calls need a token, and nothing else does. read_token holds the value for the
# function that re-exports it, and the export goes away here, so no other command sees it.
read_token="${REPORT_GH_TOKEN:-}"
unset REPORT_GH_TOKEN

# shellcheck source=scripts/usage.sh
source "$(dirname "$0")/usage.sh"

while (($# > 0)); do
  case "$1" in
  --date)
    if (($# < 2)); then
      echo "daily-report: --date needs a value YYYY-MM-DD; see --help" >&2
      exit 2
    fi
    day="$2"
    shift 2
    ;;
  --out-dir)
    if (($# < 2)); then
      echo "daily-report: --out-dir needs a directory; see --help" >&2
      exit 2
    fi
    out_dir="$2"
    shift 2
    ;;
  --skip-unchanged)
    skip_unchanged=1
    shift
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "daily-report: unknown argument '$1'; see --help" >&2
    exit 2
    ;;
  esac
done

if [[ -z "$day" || -z "$out_dir" ]]; then
  echo "daily-report: --date and --out-dir are required; see --help" >&2
  exit 2
fi
# The day names files next to the store, so only a real date may pass.
# shellcheck source=scripts/valid-day.sh
source "$(dirname "$0")/valid-day.sh"
# shellcheck source=scripts/release-exists.sh
source "$(dirname "$0")/release-exists.sh"
if ! is_valid_day "$day"; then
  echo "daily-report: --date '$day' is not a real date YYYY-MM-DD; see --help" >&2
  exit 2
fi
if [[ -z "${HOCKEY_MODEL_DB:-}" ]]; then
  echo "daily-report: HOCKEY_MODEL_DB must name the store file; see --help" >&2
  exit 2
fi

budget="${REPORT_BUDGET_S:-600}"
if [[ ! "$budget" =~ ^[0-9]+$ ]]; then
  echo "daily-report: REPORT_BUDGET_S must be whole seconds, got '$budget'; see --help" >&2
  exit 2
fi
store_dir="$(dirname "$HOCKEY_MODEL_DB")"
mkdir -p "$out_dir"
# A marker or a report from an earlier run in this directory must not stand for this run.
rm -f "$out_dir/complete" "$out_dir/report-$day.json"
if [[ ! -f "$store_dir/prepared-$day" ]]; then
  # The prepare runs every hour. A run before the first puck drop finds no marker when the
  # prepare has not finished, and the next hour can still make it, so that run skips. After the
  # first puck drop the same missing marker still fails below.
  due_json="$(env -u GH_TOKEN uv run hockey-model report-due --date "$day")" || {
    echo "daily-report: report-due failed while checking the puck drop; its error is above" >&2
    exit 1
  }
  # A string "true" must not skip, so the field has to be a JSON boolean.
  if ! jq -e '(.puck_drop_passed | type == "boolean") and (.games | type == "number")
    and (.games_outside_window | type == "number")' <<<"$due_json" >/dev/null; then
    echo "daily-report: report-due payload lacks a boolean puck_drop_passed, games, or" \
      "games_outside_window: $due_json" >&2
    exit 1
  fi
  # A feed that lists the day under the wrong date drops every game, and a skip would hide
  # that. check-daily-report.sh refuses the same state.
  outside="$(jq -r '.games_outside_window' <<<"$due_json")"
  if [[ "$outside" != "0" && "$(jq -r '.games' <<<"$due_json")" == "0" ]]; then
    echo "daily-report: $day has no game inside its window, and the feed lists $outside" \
      "game(s) outside it. The feed date is probably wrong; check report-due's stderr" \
      "for the game ids." >&2
    exit 1
  fi
  if [[ "$(jq -r '.puck_drop_passed' <<<"$due_json")" == "false" ]]; then
    notice="daily-report: no preparation for $day yet, and the first puck drop is still ahead."
    notice+=" This run skips; the next hourly prepare can still finish it."
    echo "::notice::$notice" >&2
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      echo "$notice" >>"$GITHUB_STEP_SUMMARY"
    fi
    exit 0
  fi
  echo "daily-report: no preparation for $day in $store_dir." >&2
  echo "daily-report: run the daily-prepare workflow with date $day, then run this again." >&2
  echo "daily-report: if it ran, check that it passed and that the store came back from the cache." >&2
  exit 1
fi
for needed in "backtest-$day.json" "backtest-games-$day.csv"; do
  if [[ ! -f "$store_dir/$needed" ]]; then
    echo "daily-report: prepared-$day exists but $store_dir/$needed is missing." >&2
    echo "daily-report: run the daily-prepare workflow with date $day and --force, then run" \
      "this again." >&2
    exit 1
  fi
done

# Each step logs a start line and an end line to stderr, so the log shows where time goes. A
# failed step logs a stop line with its exit code, so the log names the step that stopped the run.
# shellcheck source=scripts/run-step.sh
source "$(dirname "$0")/run-step.sh"
RUN_STEP_COMMAND=daily-report

# The index of the day lists each build with its generated_at, and the build with the latest
# generated_at is the newest. A build name holds only the UTC time of day, so the names do not
# give the order across midnight UTC.
newest_build_filter='.builds | max_by(.generated_at)'

# Print why release $1 is behind its newest build, or print nothing when it is not (#318).
# A publish uploads the version files, then the report-DATE files and the index, then the
# notes. A failed step leaves the release behind, and so does a retry of an older build from
# before the publish script kept the report-DATE files with the newest build. The function
# downloads the index and report-DATE.json into the directory $2 for the compare. It returns 1
# when a release call fails or the index names no build.
release_gap() {
  local tag="$1" dir="$2" names asset newest_at newest_build alias_at notes
  names="$(gh release view "$tag" --json assets --jq '.assets[].name')" || {
    echo "daily-report: cannot list the assets of release $tag; its error is above" >&2
    return 1
  }
  for asset in "index-$day.json" "report-$day.json"; do
    if ! grep -qxF "$asset" <<<"$names"; then
      printf 'it has no %s' "$asset"
      return 0
    fi
  done
  for asset in "index-$day.json" "report-$day.json"; do
    if ! gh release download "$tag" --pattern "$asset" --dir "$dir" ||
      [[ ! -s "$dir/$asset" ]]; then
      echo "daily-report: cannot download $asset from release $tag; check that the release holds it" >&2
      return 1
    fi
  done
  if ! newest_at="$(jq -er "$newest_build_filter | .generated_at | select(type == \"string\")" \
    "$dir/index-$day.json")" ||
    ! newest_build="$(jq -er "$newest_build_filter | .file | select(type == \"string\")" \
      "$dir/index-$day.json")"; then
    echo "daily-report: index-$day.json of release $tag names no build; check the index of the release" >&2
    return 1
  fi
  newest_build="${newest_build%.json}"
  # A report-DATE.json that is not a report is a defect of the release, and a publish repairs it.
  if ! alias_at="$(jq -er '.generated_at | select(type == "string")' "$dir/report-$day.json" \
    2>/dev/null)"; then
    printf 'its report-%s.json holds no generated_at' "$day"
    return 0
  fi
  if [[ "$alias_at" != "$newest_at" ]]; then
    printf 'its report-%s.json is not the newest build %s' "$day" "$newest_build"
    return 0
  fi
  notes="$(gh release view "$tag" --json body --jq '.body')" || {
    echo "daily-report: cannot read the notes of release $tag; its error is above" >&2
    return 1
  }
  # publish-daily-report.sh ends the build line of the notes with "UTC, BUILD.".
  if ! grep -qF "UTC, $newest_build." <<<"$notes"; then
    printf 'its notes do not name the newest build %s' "$newest_build"
  fi
}

# Exit 0 with no complete marker when the release of the day is complete for its newest build
# and the built report has the inputs of that build, so the publish job skips. A release that
# is behind its newest build is published again, which repairs it.
skip_if_unchanged() {
  # The token reaches only the release calls of this function.
  local -x GH_TOKEN="$read_token"
  local tag="reports-$day" status compare_dir gap answer reason newest_build
  status="$(release_status "$tag")"
  case "$status" in
  published) ;;
  missing) return 0 ;;
  *)
    echo "daily-report: the release lookup for $tag failed: ${status#failed: }" >&2
    exit 1
    ;;
  esac
  compare_dir="$(mktemp -d)"
  if ! gap="$(release_gap "$tag" "$compare_dir")"; then
    rm -rf "$compare_dir"
    exit 1
  fi
  if [[ -n "$gap" ]]; then
    echo "::notice::daily-report: release $tag is behind its newest build: $gap. This build publishes to repair it." >&2
    rm -rf "$compare_dir"
    return 0
  fi
  newest_build="$(jq -r "$newest_build_filter | .file" "$compare_dir/index-$day.json")"
  answer="$(env -u GH_TOKEN uv run hockey-model report-compare \
    --built "$out_dir/report-$day.json" --published "$compare_dir/report-$day.json")" || {
    echo "daily-report: report-compare failed; its error is above" >&2
    rm -rf "$compare_dir"
    exit 1
  }
  rm -rf "$compare_dir"
  if ! reason="$(jq -er '.reason | select(type == "string")' <<<"$answer")"; then
    echo "daily-report: report-compare payload lacks a reason string: $answer" >&2
    exit 1
  fi
  if [[ "$reason" == "unchanged" ]]; then
    notice="daily-report: the inputs of the $day report are unchanged since the newest"
    notice+=" published build $newest_build. Nothing publishes."
    echo "::notice::$notice" >&2
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      echo "$notice" >>"$GITHUB_STEP_SUMMARY"
    fi
    exit 0
  fi
}

# The cache may restore a store that older code wrote, because the prepare run can save one.
# The upgrade keeps every row and writes a backup, so the steps below read a current store.
start=$SECONDS
run_step upgrade uv run hockey-model store upgrade
run_step refresh uv run hockey-model refresh --starters-only
run_step predict uv run hockey-model daily --date "$day" --out-dir "$out_dir"
if ((skip_unchanged == 1)); then
  skip_if_unchanged
fi
cp "$store_dir/backtest-$day.json" "$store_dir/backtest-games-$day.csv" "$out_dir/"
# The marker says every output file is in place. The budget check comes after it, so an
# over-budget report still carries the marker and the workflow can publish it.
touch "$out_dir/complete"

elapsed=$((SECONDS - start))
echo "daily-report: total ${elapsed}s, budget ${budget}s" >&2
if ((elapsed > budget)); then
  echo "daily-report: the report took ${elapsed}s, over the ${budget}s budget (REPORT_BUDGET_S)." >&2
  echo "daily-report: the owner needs the report inside the limit. Find the slow step in the log above." >&2
  exit 1
fi
