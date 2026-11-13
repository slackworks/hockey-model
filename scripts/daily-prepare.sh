#!/usr/bin/env bash
# Prepare one game day: refresh every source, run the backtest, and fit the model on the
# features that the backtest kept.
#
# Usage: daily-prepare.sh [--date YYYY-MM-DD] [--help]
#   --date  Eastern game day to prepare. Default: today in Eastern time.
#
# Reads the store path from HOCKEY_MODEL_DB. The first backfill runs until a marker file
# next to the store says it finished. The backfill reads one year at a time, newest year
# first, and stops after BACKFILL_BUDGET_S seconds (default 1800). A stopped backfill
# writes no marker and exits with code 75. It resumes on the next run, because refresh
# skips what it already read. A budget that stops the backfill before it reads one older
# day or game, not counting the last two days, exits with code 1, and the message names
# --budget-seconds: raise BACKFILL_BUDGET_S. The backtest files and a prepared-DATE
# marker go next to the store, where daily-report.sh finds them. Any failed step stops
# the script with its exit code, so a failed run writes no marker.
set -euo pipefail

day=""

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

while (($# > 0)); do
  case "$1" in
  --date)
    if (($# < 2)); then
      echo "daily-prepare: --date needs a value YYYY-MM-DD; see --help" >&2
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
    echo "daily-prepare: unknown argument '$1'; see --help" >&2
    exit 2
    ;;
  esac
done

# The day names files next to the store, so only a real date may pass.
# shellcheck source=scripts/valid-day.sh
source "$(dirname "$0")/valid-day.sh"
if [[ -n "$day" ]] && ! is_valid_day "$day"; then
  echo "daily-prepare: --date '$day' is not a real date YYYY-MM-DD; give an Eastern game day" >&2
  exit 2
fi
if [[ -z "${HOCKEY_MODEL_DB:-}" ]]; then
  echo "daily-prepare: HOCKEY_MODEL_DB must name the store file; see --help" >&2
  exit 2
fi
# The game day is the Eastern date, as GAME_TIME_ZONE in src/hockey_model/game_day.py says.
day="${day:-$(TZ=America/New_York date +%F)}"
store_dir="$(dirname "$HOCKEY_MODEL_DB")"
mkdir -p "$store_dir"

# The marker is written only after a backfill finishes. A store file without it is partial.
if ! command -v jq >/dev/null; then
  echo "daily-prepare: jq is not installed; it reads the refresh report and live_features" >&2
  exit 1
fi
marker="$HOCKEY_MODEL_DB.backfilled"
if [[ ! -f "$marker" ]]; then
  echo "daily-prepare: the store has no finished backfill; backfilling from 2014-10-01." >&2
  echo "daily-prepare: the backfill reads one year per window, newest first." >&2
  report="$store_dir/refresh-report.json"
  refresh_rc=0
  uv run hockey-model refresh --backfill-from 2014-10-01 \
    --budget-seconds "${BACKFILL_BUDGET_S:-1800}" >"$report" || refresh_rc=$?
  cat "$report"
  if ((refresh_rc != 0)); then
    exit "$refresh_rc"
  fi
  complete="$(jq -r '.backfill_complete' "$report")" || {
    echo "daily-prepare: cannot read $report with jq." >&2
    exit 1
  }
  case "$complete" in
  true) ;;
  false)
    echo "daily-prepare: the backfill stopped before it reached 2014-10-01; run it again." >&2
    exit 75
    ;;
  *)
    echo "daily-prepare: $report has no backfill_complete flag; the marker needs it" >&2
    exit 1
    ;;
  esac
  touch "$marker"
else
  uv run hockey-model refresh
fi

# Files from earlier days are stale. The release of each day already holds its own copy.
find "$store_dir" -maxdepth 1 -type f \( -name 'prepared-*' -o -name 'backtest-*' \) -delete
backtest_games="$store_dir/backtest-games-$day.csv"
backtest_json="$store_dir/backtest-$day.json"
uv run hockey-model backtest --games-out "$backtest_games" >"$backtest_json"
# The fit serves only the features that passed the backtest's joint test. An empty list is
# the Elo alone, and the fit command takes it as an empty --features value.
if ! live_features="$(jq -er '.live_features | if type == "array" and all(type == "string") then join(",") else error("not names") end' "$backtest_json")"; then
  echo "daily-prepare: $backtest_json has no live_features list of names; the fit needs it" >&2
  exit 1
fi
if [[ -z "$live_features" ]]; then
  echo "daily-prepare: no feature passed the joint test, so the fit is the Elo alone" >&2
fi
uv run hockey-model fit --features "$live_features"
touch "$store_dir/prepared-$day"
