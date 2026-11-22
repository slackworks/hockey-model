"""Tests that specs/store_upgrade.cfg matches the store upgrade code, and that the spec is whole."""

import itertools
import re
from pathlib import Path

import pytest
from mutation_copy import require_path
from test_tla_constants import parse_constants

from hockey_model.store.db import SCHEMA_VERSION
from hockey_model.store.migrations import MIGRATIONS
from hockey_model.store.shape import REQUIREMENTS

ROOT = Path(__file__).resolve().parents[1]
CFG = require_path(ROOT / "specs" / "store_upgrade.cfg")
SPEC = require_path(ROOT / "specs" / "store_upgrade.tla")


def _constants() -> dict[str, str]:
    return parse_constants(CFG.read_text(encoding="utf-8"), "specs/store_upgrade.cfg")


def _set(raw: str) -> set[int]:
    match = re.fullmatch(r"\{([0-9, ]*)\}", raw)
    if match is None:
        pytest.fail(f"{raw!r} is not a set of integers")
    return {int(item) for item in match.group(1).split(",") if item.strip()}


def test_the_schema_version_of_the_config_is_the_schema_version_of_the_code() -> None:
    assert int(_constants()["SchemaVersion"]) == SCHEMA_VERSION


def test_the_migrations_are_a_chain_from_version_one_to_the_version_before_the_current() -> None:
    assert sorted(MIGRATIONS) == list(range(1, SCHEMA_VERSION))


def test_the_requirements_of_the_config_are_the_requirements_of_the_shape_probe() -> None:
    expected: set[int] = set()
    ordered = sorted(REQUIREMENTS, key=lambda r: r.version)
    for version, group in itertools.groupby(ordered, key=lambda r: r.version):
        expected |= {10 * version + index for index, _ in enumerate(group)}
    assert _set(_constants()["Reqs"]) == expected


def test_every_version_after_the_first_adds_a_requirement() -> None:
    # The spec assumes it, because a version with none would have no step to crash in.
    versions = {r.version for r in REQUIREMENTS}
    assert versions == set(range(2, SCHEMA_VERSION + 1))


def test_every_invariant_and_property_of_the_config_is_defined_in_the_spec() -> None:
    names = re.findall(r"^ {4}([A-Z][A-Za-z]+)$", CFG.read_text(encoding="utf-8"), re.MULTILINE)
    spec = SPEC.read_text(encoding="utf-8")
    assert names
    for name in names:
        assert re.search(rf"^{name}\b.*==", spec, re.MULTILINE), f"{name} is not defined"


def test_the_config_checks_every_invariant_that_the_spec_defines() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    defined = set(re.findall(r"^([A-Z][A-Za-z]+) ==", spec, re.MULTILINE))
    helpers = {
        "Init", "Next", "Spec", "Total", "Holding", "Other", "Tx", "Procs_", "Versions",
        "TakeLock", "LockBusy", "ReadVersion", "CopyRefused", "CopyBegin", "CopyEnd",
        "ApplyUnit", "NextVersion", "Views", "Probe", "WriteLedger", "WriteStamp", "Commit",
        "Fail", "Crash", "OperatorMovesCopy", "Writer", "Needs", "Cum", "Earned", "StartOf",
    }  # fmt: skip
    cfg = CFG.read_text(encoding="utf-8")
    checked = set(re.findall(r"^ {4}([A-Z][A-Za-z]+)$", cfg, re.MULTILINE))
    assert defined - helpers <= checked, sorted(defined - helpers - checked)
