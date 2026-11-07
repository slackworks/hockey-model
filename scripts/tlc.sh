#!/usr/bin/env bash
# Run TLC on the daily prepare spec. The jar is pinned by SHA-256, and the copy that runs is the
# one that the checksum covered.
#
# Usage: bash scripts/tlc.sh [--help]
#   --help  Print this usage and exit 0.
#
# Environment:
#   TLA_TOOLS_DIR  Directory for the jar. Default: $HOME/.cache/hockey-model/tla.
#
# Exit codes: 0 when every property holds. 12 for a violated invariant. 13 for a violated
# action or temporal property. 11 for a deadlock. 2 for an unknown argument. 1 for a setup
# failure in this script, or for a JVM crash. Any other code comes from TLC, and the output
# above names the cause.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TLA_TOOLS_VERSION="v1.7.4"
TLA_TOOLS_SHA256="936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
TLA_TOOLS_DIR="${TLA_TOOLS_DIR:-$HOME/.cache/hockey-model/tla}"
TLA_URL="https://github.com/tlaplus/tlaplus/releases/download/$TLA_TOOLS_VERSION/tla2tools.jar"
JAR="$TLA_TOOLS_DIR/tla2tools-$TLA_TOOLS_VERSION.jar"

usage() {
  cat <<'EOF'
Usage: bash scripts/tlc.sh [--help]
Run TLC on specs/daily_prepare.tla with specs/daily_prepare_stall.cfg and specs/daily_prepare.cfg.
  --help  Print this usage and exit 0.
Environment:
  TLA_TOOLS_DIR  Directory for the jar. Default: $HOME/.cache/hockey-model/tla.
Exit codes: 0 when every property holds. 12 for a violated invariant. 13 for a violated
action or temporal property. 11 for a deadlock. 2 for an unknown argument. 1 for a setup
failure in this script, or for a JVM crash. Other codes come from TLC.
EOF
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d ' ' -f 1
  else
    shasum -a 256 "$1" | cut -d ' ' -f 1
  fi
}

if (($# > 1)); then
  echo "tlc.sh: unexpected argument '$2'; run with --help for the usage" >&2
  exit 2
fi
case "${1:-}" in
  "") ;;
  --help)
    usage
    exit 0
    ;;
  *)
    echo "tlc.sh: unknown argument '$1'; run with --help for the usage" >&2
    exit 2
    ;;
esac

if ! command -v java >/dev/null 2>&1; then
  echo "tlc.sh: java is not on PATH. Install Java 11 or later." >&2
  exit 1
fi

# An older JDK fails later with a class-version error, so the version is checked here.
JAVA_MIN_MAJOR=11
# The match is anchored to the "version" line, so a quoted number in another line is ignored.
# Java 8 and older report "1.8.0_292", so group 3 is the major number in both forms.
version_re='(openjdk|java) version "(1\.)?([0-9]+)'
raw_version="$(java -version 2>&1 || true)"
if [[ ! "$raw_version" =~ $version_re ]]; then
  echo "tlc.sh: cannot read the Java version from 'java -version'." >&2
  echo "  First line of its output: ${raw_version%%$'\n'*}" >&2
  echo "  Install Java $JAVA_MIN_MAJOR or later." >&2
  exit 1
fi
java_major="${BASH_REMATCH[3]}"
if ((java_major < JAVA_MIN_MAJOR)); then
  echo "tlc.sh: Java $java_major is too old for TLC." >&2
  echo "  Install Java $JAVA_MIN_MAJOR or later." >&2
  exit 1
fi

if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
  echo "tlc.sh: neither sha256sum nor shasum is on PATH." >&2
  echo "  Install coreutils, or use the shasum that ships with macOS." >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$TLA_TOOLS_DIR" || {
  echo "tlc.sh: cannot create the jar directory $TLA_TOOLS_DIR." >&2
  echo "  Set TLA_TOOLS_DIR to a writable directory." >&2
  exit 1
}
if [[ ! -f "$JAR" ]]; then
  if ! curl -fsSL -o "$tmp/download.jar" "$TLA_URL"; then
    echo "tlc.sh: cannot download tla2tools $TLA_TOOLS_VERSION from $TLA_URL." >&2
    echo "  Check the network, then run the script again." >&2
    exit 1
  fi
  mv "$tmp/download.jar" "$JAR"
fi

# Verify and run one private copy. Checking the cached file and then running it would leave a
# gap in which another user of a shared TLA_TOOLS_DIR could replace it.
cp "$JAR" "$tmp/tla2tools.jar" || {
  echo "tlc.sh: cannot copy $JAR to a private directory." >&2
  exit 1
}
actual="$(sha256_of "$tmp/tla2tools.jar")" || {
  echo "tlc.sh: cannot compute the SHA-256 of the private copy of $JAR." >&2
  exit 1
}
if [[ "$actual" != "$TLA_TOOLS_SHA256" ]]; then
  rm -f "$JAR"
  echo "tlc.sh: checksum mismatch for $JAR; the file was deleted." >&2
  echo "  expected: $TLA_TOOLS_SHA256" >&2
  echo "  actual:   $actual" >&2
  exit 1
fi

# One spec, two configs. The small stall config runs first, so a mistake fails in seconds.
rc=0
for cfg in specs/daily_prepare_stall.cfg specs/daily_prepare.cfg; do
  echo "tlc.sh: checking specs/daily_prepare.tla with $cfg"
  mkdir -p "$tmp/states/$(basename "$cfg" .cfg)"
  java -XX:+UseParallelGC -cp "$tmp/tla2tools.jar" tlc2.TLC -workers auto \
    -metadir "$tmp/states/$(basename "$cfg" .cfg)" -config "$cfg" specs/daily_prepare.tla ||
    rc=$?
  if ((rc != 0)); then
    echo "tlc.sh: the check of $cfg failed." >&2
    break
  fi
done

case "$rc" in
  0) ;;
  11) echo "tlc.sh: TLC found a deadlock (exit 11). The trace is above." >&2 ;;
  12)
    echo "tlc.sh: TLC found a violated invariant (exit 12)." >&2
    echo "  The counterexample trace is above." >&2
    ;;
  13)
    echo "tlc.sh: TLC found a violated action or temporal property (exit 13)." >&2
    echo "  The counterexample trace is above." >&2
    ;;
  *) echo "tlc.sh: TLC failed with exit $rc. No counterexample: read the TLC error above." >&2 ;;
esac
exit "$rc"
