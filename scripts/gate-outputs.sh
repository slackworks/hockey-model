#!/usr/bin/env bash
# Print the outputs of the daily-report gate step as KEY=VALUE lines for $GITHUB_OUTPUT.
#
# Usage: gate-outputs.sh GATE_JSON [--help]
#   GATE_JSON  The JSON that scripts/report-gate.sh printed.
#
# Output on stdout: due=true|false, publish=true|false, day=YYYY-MM-DD.
# The free-text reason stays out of the output, because a newline in it could add a line.
# Exit 0 when every field has its type. Exit 1, with nothing on stdout, when a field is
# missing, has the wrong type, or the date is not YYYY-MM-DD. Exit 2 for a bad argument.
set -euo pipefail

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

file=""
while (($# > 0)); do
  case "$1" in
  -h | --help)
    usage
    exit 0
    ;;
  -*)
    echo "gate-outputs: unknown argument '$1'; see --help" >&2
    exit 2
    ;;
  *)
    if [[ -n "$file" ]]; then
      echo "gate-outputs: pass one GATE_JSON file; see --help" >&2
      exit 2
    fi
    file="$1"
    shift
    ;;
  esac
done
if [[ -z "$file" || ! -r "$file" ]]; then
  echo "gate-outputs: GATE_JSON must be a readable file; see --help" >&2
  exit 2
fi

# jq -e exits 1 for a false value, so the type test returns a boolean and the value is read apart.
require() {
  local name="$1" type="$2"
  if ! jq -e --arg name "$name" --arg type "$type" '.[$name] | type == $type' "$file" >/dev/null 2>&1; then
    echo "gate-outputs: field '$name' of $file must be a $type; the gate printed: $(head -c 300 "$file")" >&2
    exit 1
  fi
}
require due boolean
require publish boolean
require date string
require reason string

day="$(jq -r '.date' "$file")"
if [[ ! "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "gate-outputs: field 'date' of $file must be YYYY-MM-DD, got '$day'" >&2
  exit 1
fi
echo "due=$(jq -r '.due' "$file")"
echo "publish=$(jq -r '.publish' "$file")"
echo "day=$day"
