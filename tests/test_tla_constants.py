"""Tests that the constants in the specs/daily_prepare*.cfg files match the values in the code."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

from hockey_model.store.refresh_context import MAX_UNIT_FAILURES
from hockey_model.store.refresh_nhl import BACKLOG_BEFORE, RETRY_FAILED_AFTER
from hockey_model.store.source_reads import ABANDON_AFTER_FAILED_READS, ABANDON_MIN_SPAN_DAYS

ROOT = Path(__file__).resolve().parents[1]
CFGS = (
    ROOT / "specs" / "daily_prepare.cfg",
    ROOT / "specs" / "daily_prepare_stall.cfg",
    ROOT / "specs" / "daily_prepare_blocked.cfg",
    ROOT / "specs" / "daily_prepare_false_stall.cfg",
    ROOT / "specs" / "daily_prepare_cutoff.cfg",
)
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
    require_path(ROOT / "specs")
    source = str(cfg.relative_to(ROOT))
    return parse_constants(cfg.read_text(encoding="utf-8"), source), source


cfgs = pytest.mark.parametrize("cfg", CFGS, ids=lambda path: path.name)


def test_every_config_in_specs_is_checked_here() -> None:
    present = {path.name for path in require_path(ROOT / "specs").glob("*.cfg")}
    listed = {path.name for path in CFGS}
    assert present == listed, (
        f"specs/ and CFGS differ: only in specs/ {sorted(present - listed)}, "
        f"only in CFGS {sorted(listed - present)}; update CFGS in {Path(__file__).name}"
    )


@cfgs
def test_a_config_names_something_to_check(cfg: Path) -> None:
    # An empty INVARIANTS or PROPERTIES section makes TLC exit 0 and check nothing.
    names = [
        line.strip()
        for line in cfg.read_text(encoding="utf-8").splitlines()
        if line.startswith("    ") and "=" not in line
    ]
    assert names, f"{cfg.name} has no invariant and no property to check"


@cfgs
def test_abandon_reads_matches_code(cfg: Path) -> None:
    values, source = _load(cfg)
    assert _integer(values, "AbandonReads", source) == ABANDON_AFTER_FAILED_READS


@cfgs
def test_abandon_ticks_is_one(cfg: Path) -> None:
    # The model counts one failed read per tick. The span rule lives in the code, not here.
    values, source = _load(cfg)
    assert _integer(values, "AbandonTicks", source) == 1


_TICK_WINDOWS = (
    ("RetryTicks", "RETRY_FAILED_AFTER", RETRY_FAILED_AFTER),
    ("CutoffTicks", "BACKLOG_BEFORE", BACKLOG_BEFORE),
)
# The main config cuts the cutoff short. A longer cutoff multiplies its states (#188), and the
# other configs check the full cutoff. The short cutoff may be any length up to the code cutoff.
SHORT_CUTOFF = (ROOT / "specs" / "daily_prepare.cfg", "CutoffTicks")


@cfgs
@pytest.mark.parametrize(("name", "constant", "window"), _TICK_WINDOWS, ids=("retry", "cutoff"))
def test_ticks_match_the_code_window(
    cfg: Path, name: str, constant: str, window: timedelta
) -> None:
    values, source = _load(cfg)
    ticks = _integer(values, name, source)
    span = timedelta(days=ABANDON_MIN_SPAN_DAYS) * ticks
    if (cfg, name) == SHORT_CUTOFF:
        assert 1 <= ticks and span <= window, (
            f"{name} in {source} is {ticks}; it must be 1 or more, and "
            f"{name} * ABANDON_MIN_SPAN_DAYS ({span}) must not pass {constant} ({window})"
        )
        return
    assert span == window, (
        f"{name} * ABANDON_MIN_SPAN_DAYS is {span}, but {constant} is {window}; "
        f"update {name} in {source}"
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


_NAMES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=12
)
_VALUES = st.text(alphabet="abcXYZ0123456789{}, ", min_size=1, max_size=20).map(str.strip)


@given(constants=st.dictionaries(_NAMES, _VALUES.filter(bool), min_size=1, max_size=8))
def test_parse_returns_every_constant_line_that_it_was_given(constants: dict[str, str]) -> None:
    lines = [f"    {name} = {value}" for name, value in constants.items()]
    text = "\n".join(["CONSTANTS", *lines, "INVARIANTS", "    TypeOK", ""])
    assert parse_constants(text, "x.cfg") == constants


@given(text=st.text(max_size=200))
def test_parse_returns_a_mapping_or_fails_with_a_message(text: str) -> None:
    try:
        values = parse_constants(text, "x.cfg")
    except pytest.fail.Exception as error:
        assert "x.cfg" in str(error)
    else:
        assert all(isinstance(name, str) and isinstance(raw, str) for name, raw in values.items())


@given(
    count=st.integers(min_value=0, max_value=5),
    garbage=st.text(alphabet="!@#$%^&*()", min_size=1, max_size=10),
)
def test_a_line_that_does_not_fit_fails_and_names_its_number(count: int, garbage: str) -> None:
    lines = [f"    Name{'x' * n} = {n}" for n in range(count)]
    text = "\n".join(["CONSTANTS", *lines, f"    {garbage}", "INVARIANTS", ""])
    with pytest.raises(pytest.fail.Exception, match=rf"x\.cfg:{count + 2}: cannot read"):
        parse_constants(text, "x.cfg")
