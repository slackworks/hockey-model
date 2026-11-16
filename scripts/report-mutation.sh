#!/usr/bin/env bash
# Files, updates, or comments on the one open mutation report for a run.
#
# Usage: scripts/report-mutation.sh --stats FILE --execution success|failure --run-url URL
#                                   [--download-outcome success|failure] [--file-issue]
#
# A regression or an execution failure opens the report, or comments on the open one.
# A clean run comments a recovery note on an open report and leaves it open, so the
# owner closes it. Run only from a job that holds issues: write on the default branch.
#
# --download-outcome says whether the artifact download worked. Without it, a failed
# download reads the same as a shard that wrote no stats file.
# --file-issue makes the issue calls. Without it the script only prints the status, so a
# run on any other ref still judges its own stats and fails on a bad one.
#
# Stdout: the status word (clean, regression, or execution_failed).
# Stderr: diagnostics. Exit 0 when the report is filed. Exit 2 on bad arguments.
set -euo pipefail

# A flag that takes a value must have one. Without this, `shift 2` fails with no message.
need_value() {
  if (($# < 2)); then
    echo "report-mutation: $1 needs a value; see the usage line" >&2
    exit 2
  fi
}

# gh --limit caps a list at 1000 issues. A marked report past the cap would be missed.
readonly LIST_LIMIT=1000
STATS=""
EXECUTION=""
RUN_URL=""
DOWNLOAD="unknown"
FILE_ISSUE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
  --stats)
    need_value "$@"
    STATS="${2:-}"
    shift 2
    ;;
  --execution)
    need_value "$@"
    EXECUTION="${2:-}"
    shift 2
    ;;
  --run-url)
    need_value "$@"
    RUN_URL="${2:-}"
    shift 2
    ;;
  --download-outcome)
    need_value "$@"
    DOWNLOAD="${2:-}"
    shift 2
    ;;
  --file-issue)
    FILE_ISSUE=1
    shift
    ;;
  --help | -h)
    sed -n '2,19p' "$0"
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

# The download outcome joins the gate that decides the status. A failed download names itself
# in the reason, so it reads differently from a shard that wrote no stats file.
reason=""
if [[ "$DOWNLOAD" == "failure" ]]; then
  EXECUTION=failure
  reason="the artifact download failed"
fi

# One call decides the status and writes the body, so both read the same stats.
status="$(uv run python3 "$HELPER" status --stats "$STATS" --execution "$EXECUTION" \
  --out "$BODY" --run-url "$RUN_URL" ${reason:+--reason "$reason"})"

if ((FILE_ISSUE == 0)); then
  echo "$status"
  exit 0
fi
title="$(uv run python3 "$HELPER" title)"

if ! issues="$(gh issue list --state open --limit "$LIST_LIMIT" --json number,body,author)"; then
  echo "report-mutation: gh issue list failed; run gh auth status, then rerun" >&2
  exit 1
fi
count="$(printf '%s' "$issues" | uv run python3 "$HELPER" count)"
if [[ "$count" -ge "$LIST_LIMIT" ]]; then
  echo "report-mutation: gh issue list returned $count open issues, which is the $LIST_LIMIT cap;" >&2
  echo "report-mutation: the open report may be past the cap. Close some issues, then rerun." >&2
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
