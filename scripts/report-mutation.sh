#!/usr/bin/env bash
# Files, updates, or comments on the one open mutation report for a run.
#
# Usage: scripts/report-mutation.sh --stats FILE --run-url URL
#         (--execution success|failure | --gate-result R --shard-result R
#          --merge-result R --export-result R --download-result R)
#         [--reason TEXT]... [--file-issue]
#        scripts/report-mutation.sh --status S --body FILE --run-url URL --file-issue
#
# A regression or an execution failure opens the report, or comments on the open one.
# A clean run comments a recovery note on an open report and leaves it open, so the
# owner closes it. Run only from a job that holds issues: write on the default branch.
#
# Each --*-result names the outcome of one job, or unknown when it did not run. Each
# --reason names one cause that the counts do not show. Both reach the issue body, so a
# partial run never reads as a clean one.
# --file-issue makes the issue calls. Without it the script prints the status, so a run on
# any other ref still judges its own stats. A status that is not clean then also says on
# stderr that the step files no issue.
# --status with --body takes a decision that another job made, so only one job reads the
# stats. The body must be the file that decision wrote.
#
# Stdout: the status word (clean, regression, or execution_failed).
# Stderr: diagnostics. Exit 0 when the status is judged, and with --file-issue, the report
# is filed or updated. Exit 1 when a gh call fails. Exit 2 on bad arguments.
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
readonly OUTCOMES="success|failure|cancelled|skipped|unknown"
STATS=""
EXECUTION=""
RUN_URL=""
FILE_ISSUE=0
GIVEN_STATUS=""
GIVEN_BODY=""
REASONS=()
declare -A RESULTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
  --stats)
    need_value "$@"
    STATS="${2:-}"
    shift 2
    ;;
  --execution)
    need_value "$@"
    case "${2:-}" in
    success | failure) EXECUTION="${2:-}" ;;
    *)
      echo "report-mutation: --execution must be success or failure, got ${2:-}" >&2
      exit 2
      ;;
    esac
    shift 2
    ;;
  --run-url)
    need_value "$@"
    RUN_URL="${2:-}"
    shift 2
    ;;
  --gate-result | --shard-result | --merge-result | --export-result | --download-result)
    need_value "$@"
    if [[ ! "${2:-}" =~ ^($OUTCOMES)$ ]]; then
      echo "report-mutation: $1 must be one of $OUTCOMES, got ${2:-}" >&2
      exit 2
    fi
    # The key is the job name alone, with the leading dashes and the suffix removed.
    key="${1#--}"
    RESULTS["${key%-result}"]="${2:-}"
    shift 2
    ;;
  --reason)
    need_value "$@"
    REASONS+=("${2:-}")
    shift 2
    ;;
  --status)
    need_value "$@"
    case "${2:-}" in
    clean | regression | execution_failed) GIVEN_STATUS="${2:-}" ;;
    *)
      echo "report-mutation: --status must be clean, regression, or execution_failed," \
        "got ${2:-}" >&2
      exit 2
      ;;
    esac
    shift 2
    ;;
  --body)
    need_value "$@"
    GIVEN_BODY="${2:-}"
    shift 2
    ;;
  --file-issue)
    FILE_ISSUE=1
    shift
    ;;
  --help | -h)
    sed -n '2,20p' "$0"
    exit 0
    ;;
  *)
    echo "report-mutation: unknown argument: $1" >&2
    exit 2
    ;;
  esac
done

if [[ -z "$RUN_URL" ]]; then
  echo "report-mutation: --run-url is required" >&2
  exit 2
fi
if [[ -n "$GIVEN_STATUS" && -z "$GIVEN_BODY" ]]; then
  echo "report-mutation: --status needs --body, the file that decision wrote" >&2
  exit 2
fi
if [[ -z "$GIVEN_STATUS" && -z "$STATS" ]]; then
  echo "report-mutation: --stats is required; pass it, or pass --status and --body" >&2
  exit 2
fi

readonly HELPER="scripts/mutation_report.py"
mkdir -p .tmp

if [[ -n "$GIVEN_STATUS" ]]; then
  # The report job already decided, so this job reads no stats and repeats nothing.
  status="$GIVEN_STATUS"
  BODY="$GIVEN_BODY"
else
  readonly BODY_DECIDED=".tmp/mutation-report.md"
  BODY="$BODY_DECIDED"
  # One call decides the status and writes the body, so both read the same stats.
  args=(status --stats "$STATS" --out "$BODY" --run-url "$RUN_URL")
  if ((${#RESULTS[@]} > 0)); then
    for key in gate shard merge export download; do
      args+=("--$key-result" "${RESULTS[$key]:-unknown}")
    done
  else
    if [[ -z "$EXECUTION" ]]; then
      echo "report-mutation: give --execution, or the four --*-result flags" >&2
      exit 2
    fi
    args+=(--execution "$EXECUTION")
  fi
  for reason in "${REASONS[@]}"; do args+=("--reason" "$reason"); done
  # The merge job writes the list of timed-out mutants beside the stats. The report names a
  # missing list when the stats count a timed-out mutant.
  args+=(--timeouts "$(dirname "$STATS")/mutmut-timeouts.json")
  status="$(uv run python3 "$HELPER" "${args[@]}")"
fi

if ((FILE_ISSUE == 0)); then
  # This step files nothing. A non-clean status says so, so a missing flag cannot read as a
  # pass. The filing job holds issues: write, so its run does not print this line.
  if [[ "$status" != "clean" ]]; then
    echo "report-mutation: status $status; this step files no issue" >&2
  fi
  echo "$status"
  exit 0
fi
title="$(uv run python3 "$HELPER" title)"

if ! issues="$(gh issue list --state open --limit "$LIST_LIMIT" --json number,body,author)"; then
  echo "report-mutation: gh issue list failed; run gh auth status, then rerun" >&2
  exit 1
fi
if ! existing="$(printf '%s' "$issues" | uv run python3 "$HELPER" find --cap "$LIST_LIMIT")"; then
  exit 1
fi

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
