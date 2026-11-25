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
# version is never replaced. The report-DATE files, the backtest files, and the notes move to the
# newest build, the one with the latest generated_at. A retry of an older build leaves them there.
# The index-DATE.json asset lists every build (file, generated_at, code_sha, fit_sha256,
# inputs_sha256, schema_version). A rebuild adds its entry and drops none. A release from before
# the index gets an index of the new build only, and a warning names the builds it leaves out.
# The report-index command checks the report and the index against their schemas first, so a
# report that does not match stops the script before any upload.
# Only the release calls need GH_TOKEN. The script reads it once, unsets it, and gives it to
# each release call alone, so the package commands never see the write token.
# The release notes open with one line for each game: the favorite, its win chance, and the range
# of that win chance (report-summary). The range is model uncertainty only.
set -euo pipefail

# The gh calls need the write token, and the package commands need no token at all. read_token
# holds the value, and the export goes away here, so no uv command sees the token. Each gh
# call gets it back with a prefix (#313).
read_token="${GH_TOKEN:-}"
unset GH_TOKEN
# An empty token makes gh fall back to the stored login, so a local run would publish as a person.
if [[ -z "$read_token" ]]; then
  echo "publish-daily-report: GH_TOKEN is empty. Set it to the release token; without it gh uses the local login." >&2
  exit 2
fi

day=""
report_dir=""

# shellcheck source=scripts/usage.sh
source "$(dirname "$0")/usage.sh"

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
# shellcheck source=scripts/release-exists.sh
source "$(dirname "$0")/release-exists.sh"
# shellcheck source=scripts/release-gap.sh
source "$(dirname "$0")/release-gap.sh"
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
index="$report_dir/index-$day.json"
check_dir="$report_dir/published-check"
mkdir -p "$check_dir"

# The lookup only reads, so a failure here leaves the release as it was.
answer="$(GH_TOKEN="$read_token" release_status "$tag")"
case "$answer" in
published | missing) ;;
*)
  echo "publish-daily-report: the release lookup for $tag failed: ${answer#failed: }" >&2
  exit 1
  ;;
esac

existing_assets=""
existing_index=""
if [[ "$answer" == "published" ]]; then
  existing_assets="$(GH_TOKEN="$read_token" gh release view "$tag" --json assets \
    --jq '.assets[].name')" || {
    echo "publish-daily-report: cannot list the assets of $tag; gh's error is above. Nothing was published." >&2
    exit 1
  }
  if grep -qxF "index-$day.json" <<<"$existing_assets"; then
    if ! GH_TOKEN="$read_token" gh release download "$tag" --pattern "index-$day.json" \
      --dir "$check_dir" --clobber; then
      echo "publish-daily-report: cannot download index-$day.json from $tag; gh's error is above. Nothing was published. Run the report workflow again." >&2
      exit 1
    fi
    existing_index="$check_dir/index-$day.json"
    if [[ ! -s "$existing_index" ]]; then
      echo "publish-daily-report: the download of index-$day.json from $tag is empty. Nothing was published. Run the report workflow again." >&2
      exit 1
    fi
  fi
fi

# Exit 0 with no upload when the release is complete and this build adds nothing (#334). A
# complete release is left alone when its newest build has the same inputs, or when the first
# puck drop has passed. A release that is behind its newest build is repaired, as before.
skip_if_complete() {
  local dir="$1" gap answer reason due
  rm -rf "$dir"
  mkdir -p "$dir"
  if ! gap="$(GH_TOKEN="$read_token" release_gap "$tag" "$dir" "$day")"; then
    exit 1
  fi
  if [[ -n "$gap" ]]; then
    return 0
  fi
  if ! answer="$(uv run hockey-model report-compare --built "$json" \
    --published "$dir/report-$day.json")"; then
    echo "publish-daily-report: report-compare failed; its error is above. Nothing was published." >&2
    exit 1
  fi
  if ! reason="$(jq -er '.reason | select(type == "string")' <<<"$answer")"; then
    echo "publish-daily-report: report-compare payload lacks a reason string: $answer. Nothing was published." >&2
    exit 1
  fi
  if [[ "$reason" == "unchanged" ]]; then
    echo "::notice::publish-daily-report: the inputs of $tag are unchanged since its newest build. Nothing was published." >&2
    exit 0
  fi
  if ! due="$(uv run hockey-model report-due --date "$day")"; then
    echo "publish-daily-report: report-due failed while checking the puck drop; its error is above. Nothing was published." >&2
    exit 1
  fi
  if [[ "$(jq -r '.puck_drop_passed | type' <<<"$due")" != "boolean" ]]; then
    echo "publish-daily-report: report-due lacks a boolean puck_drop_passed; it printed: $due. Nothing was published." >&2
    exit 1
  fi
  if [[ "$(jq -r '.puck_drop_passed' <<<"$due")" == "true" ]]; then
    skipped="$tag is complete, the first puck drop has passed, and this build changes its inputs."
    echo "::warning::publish-daily-report: $skipped Nothing was published; the release keeps its newest build." >&2
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      echo "publish-daily-report: $skipped Nothing was published; the release keeps its newest build." >>"$GITHUB_STEP_SUMMARY"
    fi
    exit 0
  fi
  return 0
}
if [[ "$answer" == "published" ]]; then
  skip_if_complete "$check_dir/complete"
fi

# The command checks the report against its schema and the index against its own. It names the
# build file from the generated_at time of the report, so the script keeps no copy of that rule.
index_args=(--report "$json" --date "$day" --out "$index")
if [[ -n "$existing_index" ]]; then
  index_args+=(--existing "$existing_index")
fi
if ! built="$(uv run hockey-model report-index "${index_args[@]}")"; then
  echo "publish-daily-report: $json or the index of $day is not valid; its error is above. Nothing was published." >&2
  exit 1
fi
if ! version_name="$(jq -er '.file | select(type == "string")' <<<"$built")"; then
  echo "publish-daily-report: report-index printed no build file name; it printed: $built. Nothing was published." >&2
  exit 1
fi
build="${version_name%.json}"
# The newest build in the index owns the report-DATE files and the notes. A retry of an older
# build adds its version files and its index entry, and leaves the rest with the newest build.
if ! newest="$(jq -er '.builds | max_by(.generated_at) | .file | select(type == "string")' \
  "$index")"; then
  echo "publish-daily-report: the index that report-index wrote names no build. Nothing was published." >&2
  exit 1
fi
# A release from before the index has version files that the new index cannot list, because
# they hold no inputs_sha256.
unlisted=()
if [[ -n "$existing_assets" && -z "$existing_index" ]]; then
  version_pattern="^report-${day}-[0-9]{6}Z\.json$"
  while IFS= read -r name; do
    if [[ "$name" =~ $version_pattern && "$name" != "$version_name" ]]; then
      unlisted+=("${name%.json}")
    fi
  done <<<"$existing_assets"
fi
if ((${#unlisted[@]} > 0)); then
  echo "::warning::publish-daily-report: $tag has no index, so index-$day.json lists only $build. The earlier builds ${unlisted[*]} stay on the release with no index entry." >&2
fi
version_json="$report_dir/$build.json"
version_md="$report_dir/$build.md"
cp "$md" "$version_md"
cp "$json" "$version_json"
latest=("$md" "$json" "$backtest" "$backtest_games" "$index")
# The command checks the report again and prints the opening lines, one for each game.
if ! summary="$(uv run hockey-model report-summary --report "$json" | jq -er '.summary[]')"; then
  echo "publish-daily-report: cannot build the summary of $json; its error is above. Nothing was published." >&2
  exit 1
fi
notes="$report_dir/release-notes-$day.md"
{
  printf '%s\n\n' "$summary"
  echo "Every build's report is kept. Each report-$day-HHMMSSZ file set is one build, named by its"
  echo "generated_at time (UTC). The report-$day files and the backtest files are the newest build."
  if ((${#unlisted[@]} > 0)); then
    echo "The builds ${unlisted[*]} are from before index-$day.json, so the index does not list them."
  fi
  # daily-report.sh --skip-unchanged finds the newest build in this line: "UTC, BUILD.".
  echo "This build: commit $GITHUB_SHA, finished $(date -u +%Y-%m-%dT%H:%M:%SZ) UTC, $build."
  echo
  cat "$md"
} >"$notes"

# A version name is its build's generated_at, so the same name is the same build. A retry of
# that build finds its bytes already on the release and skips the upload. Other bytes under the
# same name are a collision. The function prints "new" or "same" and returns 1 on a collision,
# because a published version is never replaced.
version_status() {
  local file="$1" name
  name="$(basename "$file")"
  if ! grep -qxF "$name" <<<"$existing_assets"; then
    echo new
    return 0
  fi
  if ! GH_TOKEN="$read_token" gh release download "$tag" --pattern "$name" \
    --dir "$check_dir" --clobber; then
    echo "publish-daily-report: cannot download $name from $tag to compare it with this build; gh's error is above. Nothing was published. Run the report workflow again." >&2
    return 1
  fi
  if [[ ! -s "$check_dir/$name" ]]; then
    echo "publish-daily-report: the download of $name from $tag is empty, so it cannot be compared with this build. Nothing was published. Run the report workflow again." >&2
    return 1
  fi
  if cmp -s "$file" "$check_dir/$name"; then
    echo "publish-daily-report: $name is already on $tag with the same bytes; skipped." >&2
    echo same
    return 0
  fi
  echo "publish-daily-report: $name is already on $tag with other bytes; a version is never replaced. Two builds share one second. Run the report workflow again, so the later build gets a new generated_at second." >&2
  return 1
}

if [[ "$answer" == "published" ]]; then
  # The release exists. Both version names are checked before either upload, so a collision
  # leaves no half-published build. When this build is the newest, the alias and the backtest
  # files move to it with --clobber.
  md_state="$(version_status "$version_md")" || exit 1
  json_state="$(version_status "$version_json")" || exit 1
  if [[ "$md_state" == "new" ]]; then
    GH_TOKEN="$read_token" gh release upload "$tag" "$version_md"
  fi
  if [[ "$json_state" == "new" ]]; then
    GH_TOKEN="$read_token" gh release upload "$tag" "$version_json"
  fi
  if [[ "$newest" == "$version_name" ]]; then
    GH_TOKEN="$read_token" gh release upload "$tag" "${latest[@]}" --clobber
    GH_TOKEN="$read_token" gh release edit "$tag" --notes-file "$notes"
  else
    echo "publish-daily-report: $build is older than the newest build ${newest%.json} of $tag. Its version files and its index entry are published; the report-$day files and the notes stay with the newest build." >&2
    GH_TOKEN="$read_token" gh release upload "$tag" "$index" --clobber
  fi
else
  GH_TOKEN="$read_token" gh release create "$tag" "$version_md" "$version_json" "${latest[@]}" \
    --target "$GITHUB_SHA" \
    --title "NHL predictions $day" \
    --notes-file "$notes"
fi
