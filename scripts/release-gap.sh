#!/usr/bin/env bash
# Compare a published release with its newest build. Source this file; it runs nothing.
#
# Usage: source release-gap.sh; release_gap TAG DIR DAY
#   DAY is the report day as YYYY-MM-DD. The caller needs a gh token that reads the release.
#   Prints why the release is behind its newest build, or prints nothing when it is not.

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
  local tag="$1" dir="$2" day="$3" names asset newest_at newest_build alias_at notes
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
