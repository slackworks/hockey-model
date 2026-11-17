#!/usr/bin/env bash
# Move the store at HOCKEY_MODEL_DB to the schema version of this checkout.
#
# Usage: upgrade-store.sh [--help]
#
# Reads HOCKEY_MODEL_DB. The upgrade keeps every row and writes a backup next to the
# store. A store that is already current is a no-op that names its last backup. A store
# file that does not exist yet is skipped, because the first backfill writes a current
# version. The report goes to stderr, so a caller that reads stdout is not affected.
# Exits 0 for an upgraded store, a current store, and a missing file. Exits with the
# upgrade's own exit code when the upgrade fails, so the caller stops.
set -euo pipefail

case "${1:-}" in
-h | --help)
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit 0
  ;;
"")
  ;;
*)
  echo "upgrade-store: unknown argument '$1'; see --help" >&2
  exit 2
  ;;
esac

if [[ -z "${HOCKEY_MODEL_DB:-}" ]]; then
  echo "upgrade-store: HOCKEY_MODEL_DB must name the store file; see --help" >&2
  exit 2
fi

# A missing file is not an error. `hockey-model store upgrade` refuses to create one, and
# the first refresh writes the store at the current version anyway.
if [[ ! -f "$HOCKEY_MODEL_DB" ]]; then
  echo "upgrade-store: no store file at $HOCKEY_MODEL_DB yet; the first refresh creates it" >&2
  exit 0
fi

if ! report="$(uv run hockey-model store upgrade)"; then
  echo "upgrade-store: the store at $HOCKEY_MODEL_DB did not upgrade; the error is above" >&2
  exit 1
fi
echo "upgrade-store: $report" >&2
