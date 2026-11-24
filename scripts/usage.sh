#!/usr/bin/env bash
# Print the usage of a script. Source this file; it only defines functions.
#
# Usage: source usage.sh; usage
#   usage prints the comment block that follows the shebang line of the script that sourced it,
#   with the leading "# " cut, and stops at the first line that is not a comment.

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}
