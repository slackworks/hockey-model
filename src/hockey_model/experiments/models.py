"""Data models for experiment registry records.

Each record describes one experiment: a hypothesis, the data it uses,
and the criteria for promoting or rejecting it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from hockey_model.types import HockeyModelError

# Status values a record can hold.
STATUS_REGISTERED = "registered"
STATUS_REJECTED = "rejected"
STATUS_INCONCLUSIVE = "inconclusive"
STATUS_PROMOTED = "promoted"
VALID_STATUSES = (STATUS_REGISTERED, STATUS_REJECTED, STATUS_INCONCLUSIVE, STATUS_PROMOTED)

# Immutable fields that must not change after registration.
IMMUTABLE_FIELDS = (
    "id",
    "title",
    "hypothesis",
    "cohort",
    "data_cutoff",
    "primary_metric",
    "baseline",
    "selection_folds",
    "min_games",
    "expected_effect",
    "promotion_criterion",
    "failure_outcome",
    "candidate_features",
    "look_number",
)

# Schema keywords this validator implements. Any other keyword raises, so a record
# never passes on a rule the validator quietly ignores.
_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "type",
        "enum",
        "pattern",
        "format",
        "items",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "required",
        "properties",
    }
)
_DATE_FORMATS = frozenset({"date"})
# ``date.fromisoformat`` also accepts 20261031 and 2026-W44-6, so the shape comes first.
_DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Each JSON Schema type in the words an error message uses.
_TYPE_WORDS = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "object": "an object",
    "array": "an array",
    "null": "null",
}


@dataclass(frozen=True, slots=True)
class _Baseline:
    """The live fit used as a baseline for comparison."""

    fit_id: int
    commit: str


@dataclass(frozen=True, slots=True)
class _Result:
    """The result of one confirmation look."""

    games: int
    difference: float
    interval: tuple[float, float]
    csv_path: str
    csv_hash: str


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    """One experiment record.

    Immutable fields cannot change after registration.
    Only ``status`` and ``result`` may be updated after registration.
    """

    id: str
    title: str
    hypothesis: str
    cohort: str
    # The last day of data the candidate used. Meaning: docs/design/promotion-bar.md
    data_cutoff: str
    primary_metric: str
    baseline: _Baseline
    selection_folds: list[tuple[int, int]] | None
    min_games: int
    expected_effect: float | None
    promotion_criterion: str
    failure_outcome: str
    status: str
    candidate_features: tuple[str, ...] = ()
    look_number: int = 1
    result: _Result | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for JSON storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperimentRecord:
        """Deserialize from a dict loaded from JSON.

        Does not mutate the input dict. Raises ``HockeyModelError`` when a field has
        the wrong shape, so a bad record never escapes as a bare ``TypeError``.
        """
        try:
            return cls(**_typed_fields(data))
        except (TypeError, ValueError) as exc:
            raise HockeyModelError(f"cannot build a record from {data!r}: {exc}") from exc

    @classmethod
    def load(cls, path: Path) -> ExperimentRecord:
        """Load a record from a JSON file."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HockeyModelError(f"cannot read {path}: {exc}") from exc
        return record_from_text(text, str(path))

    def save(self, path: Path) -> None:
        """Save this record to a JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(asdict(self), indent=2, ensure_ascii=False)
        path.write_text(text + "\n", encoding="utf-8")


def _typed_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Convert the nested JSON values of a record to their dataclass types."""
    work = dict(data)
    if isinstance(work.get("baseline"), dict):
        work["baseline"] = _Baseline(**work["baseline"])
    if isinstance(work.get("result"), dict):
        result = dict(work["result"])
        result["interval"] = _as_tuple(result.get("interval"), "result.interval")
        work["result"] = _Result(**result)
    if work.get("selection_folds") is not None:
        folds = _as_tuple(work["selection_folds"], "selection_folds")
        work["selection_folds"] = [_as_tuple(f, "selection_folds item") for f in folds]
    if work.get("candidate_features") is not None:
        work["candidate_features"] = _as_tuple(work["candidate_features"], "candidate_features")
    return work


def _as_tuple(value: Any, name: str) -> tuple[Any, ...]:
    """Return a JSON array as a tuple. A string is not an array of characters."""
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{name} must be an array, got {type(value).__name__}")
    return tuple(value)


def record_from_text(text: str, source: str) -> ExperimentRecord:
    """Build a record from JSON text. ``source`` names where the text came from."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HockeyModelError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise HockeyModelError(
            f"read {source}: a record must be a JSON object, got a {type(data).__name__}"
        )
    return ExperimentRecord.from_dict(data)


def validate_against_schema(
    record: ExperimentRecord | Mapping[str, Any],
    schema: Mapping[str, Any],
    schema_name: str = "schema",
) -> None:
    """Validate a record against a JSON Schema given as a mapping.

    Raises ``HockeyModelError`` when the record does not match.
    """
    data = record.to_dict() if isinstance(record, ExperimentRecord) else record
    _validate_value(data, schema, schema_name, "$")


def _validate_value(value: Any, schema: Mapping[str, Any], schema_name: str, path: str) -> None:
    """Validate one value against one schema node."""
    unsupported = sorted(set(schema) - _SCHEMA_KEYWORDS)
    if unsupported:
        raise HockeyModelError(
            f"{path} in {schema_name}: this validator does not implement the schema "
            f"keyword(s) {', '.join(unsupported)}; add support or remove the keyword"
        )
    expected = schema.get("type")
    if expected is not None and not _type_matches(value, expected):
        names = expected if isinstance(expected, (list, tuple)) else [expected]
        wanted = " or ".join(_TYPE_WORDS[name] for name in names)
        raise HockeyModelError(f"{path}: expected {wanted}, got {type(value).__name__}")
    _check_enum(value, schema, path)
    _check_pattern(value, schema, path)
    _check_format(value, schema, path)
    _check_bound(value, schema, path)
    if isinstance(value, (list, tuple)):
        _check_array(value, schema, schema_name, path)
    elif isinstance(value, dict):
        _check_object(value, schema, schema_name, path)


def _type_matches(value: Any, expected: str | list[str]) -> bool:
    """Return True when the value is one of the schema types."""
    names = expected if isinstance(expected, (list, tuple)) else [expected]
    return any(_matches_one(value, name) for name in names)


def _matches_one(value: Any, name: str) -> bool:
    """Return True when the value is of one JSON Schema type."""
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, (list, tuple))
    if name == "null":
        return value is None
    raise HockeyModelError(f"unknown schema type {name!r}")


def _check_enum(value: Any, schema: Mapping[str, Any], path: str) -> None:
    """Check that a value is one of the enum values."""
    enum_values = schema.get("enum")
    if enum_values is not None and value not in enum_values:
        raise HockeyModelError(f"{path}: value {value!r} not in {list(enum_values)}")


def _check_pattern(value: Any, schema: Mapping[str, Any], path: str) -> None:
    """Check that a string matches the schema pattern."""
    pattern = schema.get("pattern")
    if pattern is None:
        return
    if not isinstance(value, str):
        raise HockeyModelError(
            f"{path}: pattern {pattern!r} needs a string, got {type(value).__name__}"
        )
    if not _matches_pattern(value, pattern):
        raise HockeyModelError(f"{path}: {value!r} does not match pattern {pattern!r}")


def _matches_pattern(value: str, pattern: str) -> bool:
    """Match a value against a pattern. A bad pattern is a schema bug, not a record bug."""
    try:
        return re.compile(pattern).fullmatch(value) is not None
    except re.error as exc:
        raise HockeyModelError(f"schema pattern {pattern!r} is not a valid regex: {exc}") from exc


def _check_format(value: Any, schema: Mapping[str, Any], path: str) -> None:
    """Check the declared string format. Only ``date`` is supported."""
    fmt = schema.get("format")
    if fmt is None:
        return
    if fmt not in _DATE_FORMATS:
        raise HockeyModelError(f"{path}: this validator does not implement format {fmt!r}")
    if not isinstance(value, str):
        raise HockeyModelError(f"{path}: format {fmt!r} needs a string, got {type(value).__name__}")
    if _DATE_SHAPE.fullmatch(value) is None:
        raise HockeyModelError(f"{path}: {value!r} is not a YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise HockeyModelError(f"{path}: {value!r} is not a YYYY-MM-DD date: {exc}") from exc


def _check_bound(value: Any, schema: Mapping[str, Any], path: str) -> None:
    """Check the numeric bounds the schema declares. A non-number reaches the type check first."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return
    minimum = schema.get("minimum")
    if minimum is not None and value < minimum:
        raise HockeyModelError(f"{path}: {value} is below the minimum {minimum}")
    maximum = schema.get("maximum")
    if maximum is not None and value > maximum:
        raise HockeyModelError(f"{path}: {value} is above the maximum {maximum}")


def _check_array(
    value: Any,
    schema: Mapping[str, Any],
    schema_name: str,
    path: str,
) -> None:
    """Check the length bounds of an array and validate its items."""
    minimum = schema.get("minItems")
    if minimum is not None and len(value) < minimum:
        raise HockeyModelError(f"{path}: expected at least {minimum} items, got {len(value)}")
    maximum = schema.get("maxItems")
    if maximum is not None and len(value) > maximum:
        raise HockeyModelError(f"{path}: expected at most {maximum} items, got {len(value)}")
    items_schema = schema.get("items")
    if items_schema is not None:
        for index, item in enumerate(value):
            _validate_value(item, items_schema, schema_name, f"{path}[{index}]")


def _check_object(
    value: Any,
    schema: Mapping[str, Any],
    schema_name: str,
    path: str,
) -> None:
    """Check the required keys of an object and validate its properties."""
    for name in schema.get("required", []):
        if name not in value:
            raise HockeyModelError(f"{path}: missing required field {name!r} in {schema_name}")
    for name, prop_schema in schema.get("properties", {}).items():
        if name in value:
            _validate_value(value[name], prop_schema, schema_name, f"{path}.{name}")
