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

# Print each version file of the day that the index does not list, one per line, or print
# nothing (#349). $1 holds the asset names, $2 the index file, $3 the report day as YYYY-MM-DD.
# A build has report-DAY-HHMMSSZ.json and report-DAY-HHMMSSZ.md, so each file is compared by
# its stem with the build names of the index. Returns 1 when the index or the grep fails.
unlisted_version_files() {
  local names="$1" index="$2" day="$3" listed matches name rc=0
  listed="$(jq -r '.builds[] | .file | if type == "string" then rtrimstr(".json")
    else error("a build has no file name") end' "$index")" || {
    echo "daily-report: cannot read the build names of $index; check the index" >&2
    return 1
  }
  matches="$(grep -E "^report-$day-[0-9]{6}Z\.(json|md)\$" <<<"$names")" || rc=$?
  if ((rc > 1)); then
    echo "daily-report: cannot match the asset names of the release (grep exit $rc)" >&2
    return 1
  fi
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    if ! grep -qxF -- "${name%.*}" <<<"$listed"; then
      printf '%s\n' "$name"
    fi
  done <<<"$matches"
}

# Print why release $1 is behind its newest build, or print nothing when it is not (#318).
# A publish uploads the version files, then the report-DATE files and the index, then the
# notes. A failed step leaves the release behind, and so does a retry of an older build from
# before the publish script kept the report-DATE files with the newest build. The function
# downloads the index and report-DATE.json into the directory $2 for the compare. It returns 1
# when a release call fails or the index names no build. The callers run it inside an if-guard,
# where set -e does not apply, so each fallible call ends in its own return 1 (#340).
release_gap() {
  local tag="$1" dir="$2" day="$3" names asset newest_at newest_build alias_at notes
  local orphans orphan
  names="$(gh release view "$tag" --json assets --jq '.assets[].name')" || {
    echo "daily-report: cannot list the assets of release $tag; its error is above" >&2
    return 1
  }
  for asset in "index-$day.json" "report-$day.json"; do
    if ! grep -qxF -- "$asset" <<<"$names"; then
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
  # The index names the newest build, so its version files must be assets (#341). Older builds
  # are not checked: only the newest build owns the alias and the notes.
  for asset in "$newest_build.json" "$newest_build.md"; do
    if ! grep -qxF -- "$asset" <<<"$names"; then
      printf 'it has no %s, the version file of its newest build' "$asset"
      return 0
    fi
  done
  # A publish lists only its own build, so no later publish removes an orphan version file. It is
  # therefore a warning, not a gap: a gap would republish every run and never clear (#349).
  if ! orphans="$(unlisted_version_files "$names" "$dir/index-$day.json" "$day")"; then
    echo "daily-report: cannot read the builds of index-$day.json for release $tag;" \
      "check the index" >&2
    return 1
  fi
  while IFS= read -r orphan; do
    [[ -n "$orphan" ]] || continue
    echo "::warning::daily-report: release $tag holds $orphan, which its index does not list." \
      "No publish removes it; delete it by hand." >&2
  done <<<"$orphans"
  # A report-DATE.json that is not a report is a defect of the release, and a publish repairs it.
  if ! alias_at="$(jq -er '.generated_at | select(type == "string")' \
    "$dir/report-$day.json")"; then
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
