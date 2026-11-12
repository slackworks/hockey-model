"""Tests that the constants in the specs/daily_prepare*.cfg files match the values in the code."""

import re
from datetime import timedelta
from pathlib import Path

import pytest

from hockey_model.store.refresh_context import MAX_UNIT_FAILURES
from hockey_model.store.refresh_nhl import RETRY_FAILED_AFTER
from hockey_model.store.source_reads import ABANDON_AFTER_FAILED_READS, ABANDON_MIN_SPAN_DAYS

ROOT = Path(__file__).resolve().parents[1]
CFGS = (ROOT / "specs" / "daily_prepare.cfg", ROOT / "specs" / "daily_prepare_stall.cfg")
# One shape per line: the name, an equals sign, and the value. Meaning is checked in code.
_CONSTANT_LINE = re.compile(r"^\s*([A-Za-z]+)\s*=\s*(.+?)\s*$")
_BLOCK_ENDS = {"INVARIANTS", "PROPERTIES", "SPECIFICATION"}


def parse_constants(text: str, source: str) -> dict[str, str]:
    """Return the raw value of each ``NAME = value`` line in the CONSTANTS block.

    Fail, and name the line, on a line that does not fit the shape, on a repeated name, and
    on a file with no CONSTANTS block. A silent skip would report a present constant as missing.
    """
    values: dict[str, str] = {}
    in_block = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not in_block:
            in_block = stripped == "CONSTANTS"
            continue
        if stripped in _BLOCK_ENDS:
            break
        if not stripped:
            continue
        match = _CONSTANT_LINE.match(line)
        if match is None:
            pytest.fail(f"{source}:{number}: cannot read {stripped!r}; expected NAME = value")
        name, raw = match.group(1), match.group(2)
        if name in values:
            pytest.fail(f"{source}:{number}: {name} is listed twice in CONSTANTS")
        values[name] = raw
    if not in_block:
        pytest.fail(f"{source}: no CONSTANTS block")
    return values


def _integer(values: dict[str, str], name: str, source: str) -> int:
    """Return the integer value of a constant, or fail and name the constant and the file."""
    raw = values.get(name)
    if raw is None:
        pytest.fail(f"{name} is missing from {source}")
    if not re.fullmatch(r"[0-9]+", raw):
        pytest.fail(f"{name} in {source} is {raw!r}, not an integer")
    return int(raw)


def _load(cfg: Path) -> tuple[dict[str, str], str]:
    # The mutation run copies src and tests only, so the cfg files are not there to read.
    if "mutants" in Path(__file__).resolve().parts:
        pytest.skip("the mutation copy has no specs directory")
    source = str(cfg.relative_to(ROOT))
    return parse_constants(cfg.read_text(encoding="utf-8"), source), source


cfgs = pytest.mark.parametrize("cfg", CFGS, ids=lambda path: path.name)


@cfgs
def test_abandon_reads_matches_code(cfg: Path) -> None:
    values, source = _load(cfg)
    assert _integer(values, "AbandonReads", source) == ABANDON_AFTER_FAILED_READS


@cfgs
def test_abandon_ticks_is_one(cfg: Path) -> None:
    # The model counts one failed read per tick. The span rule lives in the code, not here.
    values, source = _load(cfg)
    assert _integer(values, "AbandonTicks", source) == 1


@cfgs
def test_retry_ticks_match_retry_window(cfg: Path) -> None:
    values, source = _load(cfg)
    span = timedelta(days=ABANDON_MIN_SPAN_DAYS) * _integer(values, "RetryTicks", source)
    assert span == RETRY_FAILED_AFTER, (
        f"RetryTicks * ABANDON_MIN_SPAN_DAYS is {span}, but RETRY_FAILED_AFTER is "
        f"{RETRY_FAILED_AFTER}; update RetryTicks in {source}"
    )


@cfgs
def test_max_fails_is_within_code_limit(cfg: Path) -> None:
    values, source = _load(cfg)
    max_fails = _integer(values, "MaxFails", source)
    assert 1 <= max_fails <= MAX_UNIT_FAILURES, (
        f"MaxFails is {max_fails} in {source}; it must be between 1 and MAX_UNIT_FAILURES "
        f"({MAX_UNIT_FAILURES})"
    )


_BLOCK = "CONSTANTS\n    AbandonReads = 2\n    MaxFails = 1\nINVARIANTS\n    TypeOK\n"


def test_parse_reads_a_well_formed_block() -> None:
    assert parse_constants(_BLOCK, "x.cfg") == {"AbandonReads": "2", "MaxFails": "1"}


def test_missing_constant_fails_with_name() -> None:
    with pytest.raises(pytest.fail.Exception, match="RetryTicks is missing from x.cfg"):
        _integer(parse_constants(_BLOCK, "x.cfg"), "RetryTicks", "x.cfg")


def test_non_integer_constant_fails_with_name() -> None:
    values = parse_constants("CONSTANTS\n    AbandonReads = two\n", "x.cfg")
    with pytest.raises(pytest.fail.Exception, match="AbandonReads in x.cfg is 'two', not an int"):
        _integer(values, "AbandonReads", "x.cfg")


def test_name_with_underscore_fails_instead_of_being_skipped() -> None:
    with pytest.raises(pytest.fail.Exception, match="x.cfg:2: cannot read 'Max_Fails = 1'"):
        parse_constants("CONSTANTS\n    Max_Fails = 1\n", "x.cfg")


def test_repeated_constant_fails_with_name() -> None:
    with pytest.raises(pytest.fail.Exception, match="AbandonReads is listed twice"):
        parse_constants("CONSTANTS\n    AbandonReads = 2\n    AbandonReads = 3\n", "x.cfg")


def test_missing_constants_block_fails() -> None:
    with pytest.raises(pytest.fail.Exception, match="no CONSTANTS block"):
        parse_constants("INVARIANTS\n    TypeOK\n", "x.cfg")
