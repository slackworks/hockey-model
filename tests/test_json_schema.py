"""Tests for the shared JSON Schema validator: the keyword guard and the plain-JSON rule."""

from datetime import date
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.json_schema import _unsupported, validate_json
from hockey_model.report_schema import REPORT_SCHEMA_FILE, _schema
from hockey_model.types import HockeyModelError

BOGUS = {"bogus": 1}
SINGLE = [
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contains",
    "not",
    "propertyNames",
]
MAPS = ["properties", "patternProperties", "$defs", "dependentSchemas"]
LISTS = ["allOf", "anyOf", "oneOf", "prefixItems"]


@pytest.mark.parametrize("key", SINGLE)
def test_an_unknown_keyword_under_a_subschema_is_found(key: str) -> None:
    assert _unsupported({key: BOGUS}) == [f"$.{key}: the keyword 'bogus' has no effect"]


def test_then_and_else_count_when_if_is_present() -> None:
    assert _unsupported({"if": {"type": "string"}, "then": {"minLength": 1}}) == []
    assert _unsupported({"if": {}, "then": BOGUS, "else": BOGUS}) == [
        "$.then: the keyword 'bogus' has no effect",
        "$.else: the keyword 'bogus' has no effect",
    ]


@pytest.mark.parametrize("key", ["then", "else"])
def test_then_and_else_without_if_have_no_effect(key: str) -> None:
    assert _unsupported({key: {"minLength": 1}}) == [f"$: the keyword '{key}' has no effect"]


@pytest.mark.parametrize("key", MAPS)
def test_an_unknown_keyword_under_a_map_of_subschemas_is_found(key: str) -> None:
    assert _unsupported({key: {"name": BOGUS}}) == [
        f"$.{key}.name: the keyword 'bogus' has no effect"
    ]


@pytest.mark.parametrize("key", LISTS)
def test_an_unknown_keyword_under_a_list_of_subschemas_is_found(key: str) -> None:
    assert _unsupported({key: [BOGUS]}) == [f"$.{key}[0]: the keyword 'bogus' has no effect"]


def test_a_keyword_that_jsonschema_does_not_enforce_is_found() -> None:
    assert _unsupported({"contentSchema": {"type": "string"}}) == [
        "$: the keyword 'contentSchema' has no effect"
    ]


def test_the_shipped_schemas_use_only_enforced_keywords() -> None:
    assert _unsupported(_schema(REPORT_SCHEMA_FILE)) == []


# One step down a schema: the keyword, and the label that the guard prints for it.
_DOWN = [
    ("items", ".items"),
    ("not", ".not"),
    ("if", ".if"),
    ("properties", ".properties.name"),
    ("allOf", ".allOf[0]"),
]


def _wrap(chain: list[tuple[str, str]], inner: dict[str, Any]) -> dict[str, Any]:
    """Return the schema that reaches ``inner`` by the steps of ``chain``."""
    schema = inner
    for key, _ in reversed(chain):
        if key == "properties":
            schema = {key: {"name": schema}}
        elif key == "allOf":
            schema = {key: [schema]}
        else:
            schema = {key: schema}
    return schema


_CHAINS = st.lists(st.sampled_from(_DOWN), max_size=4)


@given(_CHAINS)
def test_a_known_keyword_at_any_depth_is_not_reported(chain: list[tuple[str, str]]) -> None:
    assert _unsupported(_wrap(chain, {"type": "string"})) == []


@given(_CHAINS, st.sampled_from(["bogus", "minimun", "x-note"]))
def test_an_unknown_keyword_at_any_depth_is_reported_once_with_its_path(
    chain: list[tuple[str, str]], key: str
) -> None:
    path = "$" + "".join(label for _, label in chain)
    assert _unsupported(_wrap(chain, {key: 1})) == [f"{path}: the keyword '{key}' has no effect"]


_LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.text(max_size=8),
    st.just(object()),
    st.dates(),
)
_VALUES = st.recursive(
    _LEAVES,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.text(max_size=5), inner, max_size=3),
        st.tuples(inner, inner),
    ),
    max_leaves=8,
)


@given(_VALUES)
def test_any_input_returns_or_raises_the_registry_error(value: object) -> None:
    """The caller handles one error type, whatever the data holds."""
    try:
        validate_json(value, _schema(REPORT_SCHEMA_FILE), "report schema")  # type: ignore[arg-type]
    except HockeyModelError:
        pass


def _outcome(data: object, schema: dict[str, Any]) -> str | None:
    try:
        validate_json(data, schema, "test schema")
    except HockeyModelError as exc:
        return str(exc)
    return None


@given(st.lists(st.integers(min_value=-5, max_value=5), max_size=4))
def test_a_tuple_and_a_list_get_the_same_answer(values: list[int]) -> None:
    """A tuple is a JSON array once it is written, so it must not change the outcome."""
    schema = {
        "type": "object",
        "properties": {"x": {"type": "array", "items": {"type": "integer"}, "maxItems": 2}},
    }
    assert _outcome({"x": tuple(values)}, schema) == _outcome({"x": values}, schema)


@pytest.mark.parametrize("value", [date(2026, 1, 1), {1, 2}, object()])
def test_a_value_json_cannot_hold_is_named_in_the_error(value: object) -> None:
    message = _outcome({"x": value}, {"type": "object"})
    assert message is not None and "not plain JSON data" in message
