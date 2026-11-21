"""Tests for experiment registry models.

Covers:
- Schema validation with jsonschema (types, unions, patterns, bounds, formats, full messages)
- Record serialization and deserialization
- File save and load round-trip
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from hockey_model.experiments.looks import MAX_LOOKS
from hockey_model.experiments.models import (
    IMMUTABLE_FIELDS,
    ExperimentRecord,
    HockeyModelError,
    record_from_text,
    validate_against_schema,
)
from hockey_model.experiments.schemas import CHECK_OUTPUT_SCHEMA, EXPERIMENT_SCHEMA

# A valid record for testing.
_VALID_RECORD_DATA = {
    "id": "0001-test",
    "title": "Test record",
    "hypothesis": "Feature X improves log loss.",
    "cohort": "confirmation",
    "data_cutoff": "2026-10-31",
    "primary_metric": "mean_per_game_log_loss",
    "baseline": {"fit_id": 1, "commit": "abc123"},
    "selection_folds": [[20152016, 20212022]],
    "min_games": 1000,
    "candidate_features": ["xg_form"],
    "look_number": 1,
    "expected_effect": -0.003,
    "promotion_criterion": "95th percentile below zero",
    "failure_outcome": "ship Elo alone",
    "status": "registered",
}

# A minimal valid schema for testing.
_MINIMAL_SCHEMA = {
    "type": "object",
    "required": ["id", "title", "cohort", "status"],
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "cohort": {"type": "string", "enum": ["exploration", "confirmation"]},
        "status": {
            "type": "string",
            "enum": ["registered", "rejected", "inconclusive", "promoted"],
        },
        "min_games": {"type": "integer"},
    },
}


def _make_record(data: dict | None = None) -> ExperimentRecord:
    """Create a valid record from base data, overridden by data."""
    merged = {**_VALID_RECORD_DATA, **(data or {})}
    return ExperimentRecord.from_dict(merged)


class TestRecordSerialization:
    """Test record to_dict and from_dict."""

    def test_to_dict_round_trips(self):
        """A record serializes and deserializes to the same values."""
        record = _make_record()
        restored = ExperimentRecord.from_dict(record.to_dict())
        assert restored == record

    def test_json_round_trip_keeps_values(self, tmp_path: Path):
        """A record saved to JSON loads back with the same values."""
        record = _make_record()
        path = tmp_path / "0001-test.json"
        record.save(path)
        loaded = ExperimentRecord.load(path)
        assert loaded.id == record.id
        assert loaded.hypothesis == record.hypothesis
        assert loaded.baseline.commit == record.baseline.commit
        assert loaded.candidate_features == ("xg_form",)

    def test_from_dict_rejects_an_unknown_key(self):
        """An extra key raises HockeyModelError, not a bare TypeError."""
        bad = {**_VALID_RECORD_DATA, "promoted": True}
        with pytest.raises(HockeyModelError, match="cannot build a record"):
            ExperimentRecord.from_dict(bad)

    def test_result_interval_survives_json(self, tmp_path: Path):
        """A result interval keeps its two bounds through JSON."""
        record = _make_record(
            {
                "result": {
                    "games": 100,
                    "difference": -0.005,
                    "interval": [-0.010, -0.001],
                    "csv_path": "experiments/results/0001-test-games.csv",
                    "csv_hash": "0" * 64,
                }
            }
        )
        path = tmp_path / "0001-test.json"
        record.save(path)
        loaded = ExperimentRecord.load(path)
        assert loaded.result is not None
        assert loaded.result.games == 100
        assert list(loaded.result.interval) == [-0.010, -0.001]

    def test_immutable_fields_name_real_record_fields(self):
        """Every immutable field is a field of the record, so the comparison cannot miss one."""
        assert set(IMMUTABLE_FIELDS) <= set(_make_record().to_dict())

    @pytest.mark.parametrize(
        "change",
        [
            {"baseline": {"fit_id": 1}},
            {"baseline": {"fit_id": 1, "commit": "a", "extra": 1}},
            {"result": {"games": 1}},
            {
                "result": {
                    "games": 1,
                    "difference": 0.0,
                    "interval": 5,
                    "csv_path": "p",
                    "csv_hash": "h",
                }
            },
            {"selection_folds": 5},
            {"selection_folds": [5]},
            {"candidate_features": "xg_form"},
            {"candidate_features": 7},
        ],
    )
    def test_from_dict_wraps_a_wrong_shape(self, change: dict):
        """A wrong-shaped nested value raises HockeyModelError, never a bare TypeError."""
        with pytest.raises(HockeyModelError, match="cannot build a record"):
            ExperimentRecord.from_dict({**_VALID_RECORD_DATA, **change})

    def test_a_result_interval_comes_back_as_a_tuple(self):
        """The interval keeps its tuple type through to_dict and from_dict."""
        result = {
            "games": 3,
            "difference": -0.001,
            "interval": [-0.002, 0.0],
            "csv_path": "experiments/results/x.csv",
            "csv_hash": "0" * 64,
        }
        record = _make_record({"result": result})
        assert ExperimentRecord.from_dict(record.to_dict()) == record

    def test_record_from_text_rejects_a_non_object(self):
        """A JSON array is not a record."""
        with pytest.raises(HockeyModelError, match="must be a JSON object"):
            record_from_text("[1, 2]", "test input")

    def test_record_from_text_rejects_bad_json(self):
        """Broken JSON names the source of the text."""
        with pytest.raises(HockeyModelError, match="invalid JSON in test input"):
            record_from_text("{", "test input")


def _fails(data: ExperimentRecord | Mapping[str, Any], schema: dict, message: str) -> None:
    """Assert that validation fails with exactly ``message``."""
    with pytest.raises(HockeyModelError) as excinfo:
        validate_against_schema(data, schema, "test schema")
    assert str(excinfo.value) == message


_FIX = "change the value to match the schema"


def _object(**properties: dict) -> dict:
    return {"type": "object", "properties": dict(properties)}


class TestSchemaValidation:
    """Test validation against a JSON Schema with the jsonschema library."""

    def test_valid_record_passes(self):
        validate_against_schema(_make_record(), _MINIMAL_SCHEMA)

    def test_missing_required_field_names_the_field(self):
        data = _make_record().to_dict()
        del data["status"]
        _fails(
            data,
            _MINIMAL_SCHEMA,
            f"$ in test schema: 'status' is a required property (rule 'required'); {_FIX}",
        )

    def test_invalid_enum_value_lists_the_choices(self):
        record = _make_record({"cohort": "invalid_cohort"})
        _fails(
            record,
            _MINIMAL_SCHEMA,
            "$.cohort in test schema: 'invalid_cohort' is not one of "
            f"['exploration', 'confirmation'] (rule 'enum'); {_FIX}",
        )

    def test_wrong_type_names_the_path_and_the_type(self):
        bad = {**_VALID_RECORD_DATA, "min_games": "one thousand"}
        _fails(
            bad,
            _MINIMAL_SCHEMA,
            f"$.min_games in test schema: 'one thousand' is not of type 'integer' "
            f"(rule 'type'); {_FIX}",
        )

    def test_union_type_accepts_null(self):
        validate_against_schema({"result": None}, _object(result={"type": ["object", "null"]}))

    def test_union_type_rejects_a_string(self):
        _fails(
            {"result": "promoted, trust me"},
            _object(result={"type": ["object", "null"]}),
            "$.result in test schema: 'promoted, trust me' is not of type 'object', 'null' "
            f"(rule 'type'); {_FIX}",
        )

    def test_union_type_checks_object_properties(self):
        schema = _object(
            result={"type": ["object", "null"], "properties": {"games": {"type": "integer"}}}
        )
        _fails(
            {"result": {"games": "lots"}},
            schema,
            f"$.result.games in test schema: 'lots' is not of type 'integer' (rule 'type'); {_FIX}",
        )

    def test_pattern_is_enforced(self):
        schema = _object(id={"type": "string", "pattern": "^[0-9]{4}-.*$"})
        _fails(
            {"id": "xyz"},
            schema,
            "$.id in test schema: 'xyz' does not match '^[0-9]{4}-.*$' (rule 'pattern'); " + _FIX,
        )

    def test_array_bounds_are_enforced(self):
        fold = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}
        schema = _object(selection_folds={"type": "array", "items": fold})
        _fails(
            {"selection_folds": [[1, 2, 3]]},
            schema,
            f"$.selection_folds[0] in test schema: [1, 2, 3] is too long (rule 'maxItems'); {_FIX}",
        )
        _fails(
            {"selection_folds": [[1]]},
            schema,
            f"$.selection_folds[0] in test schema: [1] is too short (rule 'minItems'); {_FIX}",
        )

    def test_date_format_is_enforced(self):
        schema = EXPERIMENT_SCHEMA["properties"]["data_cutoff"]
        with pytest.raises(HockeyModelError, match=r"\$\.data_cutoff in test schema"):
            validate_against_schema(
                {"data_cutoff": "whenever"}, _object(data_cutoff=schema), "test schema"
            )

    @pytest.mark.parametrize(
        "value", ["20261031", "2026-W44-6", "2026-1-5", "2026-10-31T00:00", "2026-02-30"]
    )
    def test_the_registry_date_is_canonical_and_real(self, value: str):
        schema = _object(data_cutoff=EXPERIMENT_SCHEMA["properties"]["data_cutoff"])
        with pytest.raises(HockeyModelError, match=r"\$\.data_cutoff in test schema"):
            validate_against_schema({"data_cutoff": value}, schema, "test schema")

    def test_a_real_date_passes(self):
        schema = _object(data_cutoff=EXPERIMENT_SCHEMA["properties"]["data_cutoff"])
        validate_against_schema({"data_cutoff": "2026-10-31"}, schema)

    def test_numeric_minimum_is_enforced(self):
        schema = _object(min_games={"type": "integer", "minimum": 1})
        _fails(
            {"min_games": 0},
            schema,
            f"$.min_games in test schema: 0 is less than the minimum of 1 (rule 'minimum'); {_FIX}",
        )

    def test_a_keyword_such_as_const_is_enforced(self):
        schema = _object(id={"type": "string", "const": "0001-test"})
        _fails(
            {"id": "anything"},
            schema,
            f"$.id in test schema: '0001-test' was expected (rule 'const'); {_FIX}",
        )

    def test_a_schema_that_is_not_json_schema_is_refused(self):
        with pytest.raises(
            HockeyModelError, match=r"bad schema: the schema is not valid JSON Schema"
        ):
            validate_against_schema({}, {"type": "not-a-type"}, "bad schema")

    def test_the_two_registry_schemas_are_valid_draft_2020_12(self):
        for schema in (EXPERIMENT_SCHEMA, CHECK_OUTPUT_SCHEMA):
            Draft202012Validator.check_schema(schema)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@given(
    number=st.integers(min_value=1, max_value=9999),
    slug=st.from_regex(r"[a-z][a-z0-9-]{0,20}", fullmatch=True),
    min_games=st.integers(min_value=1, max_value=100000),
    looks=st.integers(min_value=1, max_value=MAX_LOOKS),
)
def test_a_well_formed_record_passes_the_registry_schema(
    number: int, slug: str, min_games: int, looks: int
) -> None:
    """Every record built from valid parts passes the registry schema."""
    record = _make_record(
        {
            "id": f"{number:04d}-{slug}",
            "min_games": min_games,
            "look_number": looks,
            "selection_folds": None,
        }
    )
    validate_against_schema(record, EXPERIMENT_SCHEMA)


def test_a_look_number_above_the_maximum_fails_the_registry_schema() -> None:
    record = _make_record({"look_number": MAX_LOOKS + 1})
    with pytest.raises(HockeyModelError, match=r"look_number"):
        validate_against_schema(record, EXPERIMENT_SCHEMA)


_JSON_LEAVES = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text(max_size=8)
)
_JSON_VALUES = st.recursive(
    _JSON_LEAVES,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=5), inner)
    ),
    max_leaves=6,
)


@given(text=st.text(max_size=60))
def test_record_from_text_never_raises_a_foreign_error_on_text(text: str) -> None:
    """Any text gives a record or a HockeyModelError, never another exception."""
    try:
        record_from_text(text, "fuzz")
    except HockeyModelError:
        pass


@settings(max_examples=100)
@given(
    changes=st.dictionaries(
        st.sampled_from(sorted(_VALID_RECORD_DATA)),
        _JSON_VALUES,
        max_size=4,
    )
)
def test_from_dict_never_raises_a_foreign_error_on_wrong_types(changes: dict) -> None:
    """A record with any JSON value in any field gives a record or a HockeyModelError."""
    try:
        ExperimentRecord.from_dict({**_VALID_RECORD_DATA, **changes})
    except HockeyModelError:
        pass


@given(
    games=st.integers(min_value=0, max_value=10_000),
    low=st.floats(min_value=-1, max_value=0, allow_nan=False),
    high=st.floats(min_value=0, max_value=1, allow_nan=False),
    folds=st.one_of(
        st.none(),
        st.lists(st.tuples(st.integers(1, 9), st.integers(1, 9)), max_size=3),
    ),
    with_result=st.booleans(),
)
def test_a_record_round_trips_through_json(games, low, high, folds, with_result) -> None:
    """record_from_text(json.dumps(record.to_dict())) gives back an equal record."""
    result = (
        {
            "games": games,
            "difference": (low + high) / 2,
            "interval": [low, high],
            "csv_path": "experiments/results/x.csv",
            "csv_hash": "a" * 64,
        }
        if with_result
        else None
    )
    record = _make_record(
        {
            "selection_folds": folds,
            "result": result,
            "status": "rejected" if with_result else "registered",
        }
    )
    assert record_from_text(json.dumps(record.to_dict()), "memory") == record


@given(
    expected=st.sampled_from(["string", "integer", "boolean", "array", "object"]),
    value=_JSON_VALUES,
)
def test_a_value_of_the_wrong_type_is_rejected(expected: str, value) -> None:
    """A value passes only when its JSON type is the declared one."""
    schema = {"type": "object", "properties": {"x": {"type": expected}}}
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    matches = {
        "string": isinstance(value, str),
        "integer": is_number and float(value).is_integer(),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }[expected]
    if matches:
        validate_against_schema({"x": value}, schema)
    else:
        with pytest.raises(HockeyModelError):
            validate_against_schema({"x": value}, schema)


@given(minimum=st.integers(-50, 50), value=st.integers(-100, 100))
def test_a_number_below_the_minimum_is_rejected(minimum: int, value: int) -> None:
    """A bound is enforced: the value passes if and only if it reaches the minimum."""
    schema = {"type": "object", "properties": {"x": {"type": "integer", "minimum": minimum}}}
    if value >= minimum:
        validate_against_schema({"x": value}, schema)
    else:
        with pytest.raises(HockeyModelError, match="less than the minimum"):
            validate_against_schema({"x": value}, schema)
