#!/usr/bin/env bash
# Check a game day. Source this file; it only defines functions.
#
# Usage: source valid-day.sh; is_valid_day YYYY-MM-DD
#   is_valid_day returns 0 for a real calendar date, and 1 for any other text. A date names
#   files next to the store, so the shape is checked first and the calendar second.

is_valid_day() {
  local text="$1"
  # Shape: ASCII digits only, so no path text and no Unicode digit passes.
  [[ "$text" =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})$ ]] || return 1
  # Meaning: the month and the day exist in that year. 10# stops bash from reading 08 as octal.
  local year=$((10#${BASH_REMATCH[1]})) month=$((10#${BASH_REMATCH[2]})) day=$((10#${BASH_REMATCH[3]}))
  ((year >= 1 && month >= 1 && month <= 12 && day >= 1)) || return 1
  local last=31
  case "$month" in
  4 | 6 | 9 | 11) last=30 ;;
  2)
    last=28
    if ((year % 4 == 0 && (year % 100 != 0 || year % 400 == 0))); then
      last=29
    fi
    ;;
  esac
  ((day <= last))
}
