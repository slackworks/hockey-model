#!/usr/bin/env bash
# Write the daily report for one game day: refresh the starters, then predict.
#
# Usage: daily-report.sh --date YYYY-MM-DD --out-dir DIR [--help]
#   --date     Eastern game day to predict.
#   --out-dir  Directory for the report and backtest files. Created when missing.
#
# Reads the store path from HOCKEY_MODEL_DB. The morning run of daily-prepare.sh does the
# slow work. When its prepared-DATE marker is missing, this script stops with exit code 1
# and writes no report, because the slow work does not fit in the report budget. The one
# exception is a day whose first puck drop is more than the prep window away: then the script
# skips with exit code 0, because the morning run has not had its window yet. Run the
# daily-prepare workflow for the day, then run this script again. Any failed step stops
# the script with its exit code, so a failed run writes no report.
#
# Reads REPORT_BUDGET_S, a whole number of seconds. The default is set in the budget line below.
# Each step logs its seconds to stderr. The clock has one-second resolution, so a run can pass
# the budget by less than one second and still pass this check.
# When the whole run passes the budget, the script exits 1 after it writes the report. The
# workflow still uploads and publishes that report, and the build job stays failed.
set -euo pipefail

day=""
out_dir=""

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

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
# A marker from an earlier run in this directory must not stand for this run.
rm -f "$out_dir/complete"
if [[ ! -f "$store_dir/prepared-$day" ]]; then
  # The prep job runs each morning. A run hours before the first puck drop finds no marker yet,
  # so that run skips. Inside the prep window the same missing marker still fails below.
  due_json="$(uv run hockey-model report-due --date "$day")" || {
    echo "daily-report: report-due failed while checking the prep window; its error is above" >&2
    exit 1
  }
  # A string "true" must not skip, so the field has to be a JSON boolean.
  if ! jq -e '.prep_too_early | type == "boolean"' <<<"$due_json" >/dev/null; then
    echo "daily-report: report-due payload lacks a boolean prep_too_early: $due_json" >&2
    exit 1
  fi
  if [[ "$(jq -r '.prep_too_early' <<<"$due_json")" == "true" ]]; then
    notice="daily-report: no preparation for $day yet, and the first puck drop is more than the prep window away. This run skips."
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

# Run one step and log its name and its seconds to stderr, so the log shows where time goes.
# A failed step logs its name and exit code too, so the log names the step that stopped the run.
run_step() {
  local name="$1"
  local step_start=$SECONDS
  local rc=0
  shift
  "$@" || rc=$?
  echo "daily-report: step $name took $((SECONDS - step_start))s" >&2
  if ((rc != 0)); then
    echo "daily-report: step $name failed with exit code $rc" >&2
    exit "$rc"
  fi
}

start=$SECONDS
run_step refresh uv run hockey-model refresh --starters-only
run_step predict uv run hockey-model daily --date "$day" --out-dir "$out_dir"
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
