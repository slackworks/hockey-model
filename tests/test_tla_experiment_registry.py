"""Tests that the registry config matches the registry code, and that the spec is whole."""

import re
from pathlib import Path

import pytest
from mutation_copy import require_path
from test_tla_constants import parse_constants

from hockey_model.experiments.looks import MAX_LOOKS
from hockey_model.experiments.models import IMMUTABLE_FIELDS

ROOT = Path(__file__).resolve().parents[1]
CFG = require_path(ROOT / "specs" / "experiment_registry.cfg")
SPEC = require_path(ROOT / "specs" / "experiment_registry.tla")
NOTE = require_path(ROOT / "docs" / "design" / "experiment-registry-tla.md")

# The checks do not enforce these two properties. The design note records each counterexample
# and the issue that tracks it, so the config leaves them out on purpose.
KNOWN_GAPS = {"MainStaysGreen", "RegisteredBeforeRun"}
# Operators of the property section that are not properties.
HELPERS = {"OnMain", "Moved"}


def _constants() -> dict[str, str]:
    return parse_constants(CFG.read_text(encoding="utf-8"), "specs/experiment_registry.cfg")


def _properties_of_the_spec() -> set[str]:
    spec = SPEC.read_text(encoding="utf-8")
    _, _, section = spec.partition("The properties.")
    assert section, "the spec has no property section"
    return set(re.findall(r"^([A-Z][A-Za-z]+) ==", section, re.MULTILINE)) - HELPERS


def _checked() -> set[str]:
    return set(re.findall(r"^ {4}([A-Z][A-Za-z]+)$", CFG.read_text(encoding="utf-8"), re.MULTILINE))


def test_the_look_cap_of_the_config_is_the_look_cap_of_the_code() -> None:
    assert int(_constants()["MaxLooks"]) == MAX_LOOKS


def test_the_immutable_fields_of_the_config_are_the_immutable_fields_of_the_code() -> None:
    listed = re.findall(r'"([a-z_]+)"', _constants()["ImmutableFields"])
    assert sorted(listed) == sorted(IMMUTABLE_FIELDS)
    assert len(listed) == len(set(listed))


def test_the_config_has_more_record_numbers_than_looks() -> None:
    # The cap is reachable only when the model can hold one record more than it allows.
    names = {int(item) for item in re.findall(r"[0-9]+", _constants()["Valid"])}
    assert len({name // 10 for name in names}) > MAX_LOOKS


def test_every_property_of_the_config_is_defined_in_the_spec() -> None:
    checked = _checked()
    assert checked
    assert checked <= _properties_of_the_spec() | {"TypeOK"}, sorted(
        checked - _properties_of_the_spec()
    )


def test_the_config_checks_every_property_except_the_known_gaps() -> None:
    assert _properties_of_the_spec() - _checked() == KNOWN_GAPS


@pytest.mark.parametrize("name", sorted(KNOWN_GAPS))
def test_the_design_note_records_each_known_gap(name: str) -> None:
    assert name in NOTE.read_text(encoding="utf-8")


def test_the_accepted_config_models_the_code_that_the_checks_run() -> None:
    # The checks compare every commit, and they read one path with no rename detection.
    constants = _constants()
    assert constants["EveryCommit"] == "TRUE"
    assert constants["FollowRenames"] == "FALSE"
