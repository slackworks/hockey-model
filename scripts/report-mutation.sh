#!/usr/bin/env bash
# Files, updates, or comments on the one open mutation report for a run.
#
# Usage: scripts/report-mutation.sh --stats FILE --execution success|failure --run-url URL
#
# A regression or an execution failure opens the report, or comments on the open one.
# A clean run comments a recovery note on an open report and leaves it open, so the
# owner closes it. Run only from a job that holds issues: write on the default branch.
#
# Stdout: the status word (clean, regression, or execution_failed).
# Stderr: diagnostics. Exit 0 when the report is filed. Exit 2 on bad arguments.
set -euo pipefail

STATS=""
EXECUTION=""
RUN_URL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
  --stats)
    STATS="${2:-}"
    shift 2
    ;;
  --execution)
    EXECUTION="${2:-}"
    shift 2
    ;;
  --run-url)
    RUN_URL="${2:-}"
    shift 2
    ;;
  --help | -h)
    sed -n '2,11p' "$0"
    exit 0
    ;;
  *)
    echo "report-mutation: unknown argument: $1" >&2
    exit 2
    ;;
  esac
done

if [[ -z "$STATS" || -z "$EXECUTION" || -z "$RUN_URL" ]]; then
  echo "report-mutation: --stats, --execution, and --run-url are all required" >&2
  exit 2
fi

readonly HELPER="scripts/mutation_report.py"
readonly BODY=".tmp/mutation-report.md"
mkdir -p .tmp

# One call decides the status and writes the body, so both read the same stats.
status="$(uv run python3 "$HELPER" status --stats "$STATS" --execution "$EXECUTION" \
  --out "$BODY" --run-url "$RUN_URL")"
title="$(uv run python3 "$HELPER" title)"

# 1000 is the gh maximum. A marked report past that cap would be missed.
if ! issues="$(gh issue list --state open --limit 1000 --json number,body,author)"; then
  echo "report-mutation: gh issue list failed; run gh auth status, then rerun" >&2
  exit 1
fi
existing="$(printf '%s' "$issues" | uv run python3 "$HELPER" find)"

if [[ "$status" == "clean" ]]; then
  if [[ -n "$existing" ]]; then
    gh issue comment "$existing" --body "Recovery: the weekly run $RUN_URL is clean. Survivors are at or below the baseline. Close this report when you accept the result." >&2 ||
      {
        echo "report-mutation: gh issue comment #$existing failed for $RUN_URL" >&2
        exit 1
      }
  fi
elif [[ -n "$existing" ]]; then
  gh issue comment "$existing" --body-file "$BODY" >&2 ||
    {
      echo "report-mutation: gh issue comment #$existing failed for $RUN_URL" >&2
      exit 1
    }
else
  gh issue create --title "$title" --body-file "$BODY" >&2 ||
    {
      echo "report-mutation: gh issue create failed for $RUN_URL" >&2
      exit 1
    }
fi

echo "$status"
