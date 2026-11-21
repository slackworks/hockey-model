"""Tests that the appendix of the TLA+ design note holds the spec files byte for byte.

The note says an implementer copies the three files from the appendix. A change to a spec file
without the matching change to the appendix leaves the note giving a stale spec.
"""

import re
from pathlib import Path

import pytest
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
NOTE = require_path(ROOT / "docs" / "design" / "daily-prepare-tla.md")
FILES = (
    "specs/daily_prepare.tla",
    "specs/daily_prepare.cfg",
    "specs/daily_prepare_stall.cfg",
)
for _name in FILES:
    require_path(ROOT / _name)


def _appendix_block(note: str, name: str) -> str:
    """Return the fenced block that follows the heading of ``name`` in the appendix."""
    shape = re.compile(r"### `" + re.escape(name) + r"`\n\n```[a-z]*\n(.*?)\n```\n", re.DOTALL)
    match = shape.search(note)
    if match is None:
        pytest.fail(f"{NOTE.name}: the appendix has no block under the heading for {name}")
    return match.group(1)


@pytest.mark.parametrize("name", FILES)
def test_the_appendix_holds_the_file_byte_for_byte(name: str) -> None:
    block = _appendix_block(NOTE.read_text(encoding="utf-8"), name)
    assert block == (ROOT / name).read_text(encoding="utf-8").rstrip("\n"), (
        f"{name} differs from its copy in {NOTE.name}; copy the file into the appendix"
    )


def test_a_missing_block_fails_with_the_file_name() -> None:
    with pytest.raises(pytest.fail.Exception, match="no block under the heading"):
        _appendix_block("# no appendix\n", "specs/daily_prepare.tla")
