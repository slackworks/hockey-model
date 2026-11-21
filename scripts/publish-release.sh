#!/usr/bin/env bash
# Attach the built files of a release to the GitHub release of a tag (#99).
#
# Usage: publish-release.sh --tag TAG --dist DIR --notes FILE [--prerelease]
#        publish-release.sh --help
#
# DIR holds the wheel and the source archive. FILE holds the release notes. The command needs
# the gh CLI and a token that can write releases, which the publish job of release.yml passes.
#
# A new tag gets a release with the notes and every file of DIR. A tag whose release exists is
# checked first. A file with the same name and the same bytes is skipped, a new file is
# uploaded, and a file with the same name and other bytes stops the run before any upload. So a
# retry of one build is safe, and a published file is never replaced.
#
# stdout: nothing. stderr: one line for each action and for each failure.
# Exit codes: 0 after the release holds every file, 1 for a failed check or lookup, 2 for a
# bad argument.
set -euo pipefail

# shellcheck source=scripts/release-exists.sh
source "$(dirname "$0")/release-exists.sh"

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

fail() {
  echo "publish-release: $1" >&2
  exit "${2:-1}"
}

tag=""
dist=""
notes=""
prerelease=0
while (($# > 0)); do
  case "$1" in
  --tag)
    tag="${2:-}"
    shift "$(($# >= 2 ? 2 : 1))"
    ;;
  --dist)
    dist="${2:-}"
    shift "$(($# >= 2 ? 2 : 1))"
    ;;
  --notes)
    notes="${2:-}"
    shift "$(($# >= 2 ? 2 : 1))"
    ;;
  --prerelease) prerelease=1 && shift ;;
  -h | --help)
    usage
    exit 0
    ;;
  *) fail "unknown argument '$1'; run with --help" 2 ;;
  esac
done

# Shape: a tag is "v" and a digit first. check_release.py checks the meaning.
[[ "$tag" =~ ^v[0-9][0-9A-Za-z.+-]*$ ]] || fail "--tag '$tag' is not a release tag such as v0.1.0" 2
[[ -d "$dist" ]] || fail "--dist '$dist' is not a directory; build with uv build --out-dir" 2
[[ -f "$notes" ]] || fail "--notes '$notes' is not a file; write the changelog section there" 2

shopt -s nullglob
files=("$dist"/*.whl "$dist"/*.tar.gz)
((${#files[@]} == 2)) || fail "$dist must hold one wheel and one source archive, and holds ${#files[@]} file(s)"

answer="$(release_status "$tag")"
case "$answer" in
published | missing) ;;
*) fail "the release lookup for $tag failed: ${answer#failed: }" ;;
esac

if [[ "$answer" == "missing" ]]; then
  flags=(--verify-tag --title "$tag" --notes-file "$notes")
  if ((prerelease)); then
    flags+=(--prerelease)
  fi
  gh release create "$tag" "${files[@]}" "${flags[@]}"
  echo "publish-release: created the release $tag with ${#files[@]} files." >&2
  exit 0
fi

check_dir="$(mktemp -d)"
trap 'rm -rf "$check_dir"' EXIT
existing="$(gh release view "$tag" --json assets --jq '.assets[].name')"
new=()
for file in "${files[@]}"; do
  name="$(basename "$file")"
  if ! grep -qxF "$name" <<<"$existing"; then
    new+=("$file")
    continue
  fi
  gh release download "$tag" --pattern "$name" --dir "$check_dir" --clobber
  if cmp -s "$file" "$check_dir/$name"; then
    echo "publish-release: $name is already on $tag with the same bytes; skipped." >&2
  else
    fail "$name is already on $tag with other bytes; a published file is never replaced. Remove the release by hand if the new build is the right one."
  fi
done
if ((${#new[@]} > 0)); then
  gh release upload "$tag" "${new[@]}"
  echo "publish-release: uploaded ${#new[@]} file(s) to $tag." >&2
fi
