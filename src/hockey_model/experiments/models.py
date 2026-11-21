"""Data models for experiment registry records.

Each record describes one experiment: a hypothesis, the data it uses,
and the criteria for promoting or rejecting it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import SchemaError, ValidationError, best_match
from jsonschema.protocols import Validator

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
        try:
            text = json.dumps(asdict(self), indent=2, ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            raise HockeyModelError(
                f"cannot save {self.id} to {path}: {exc}; replace NaN and Infinity with a number"
            ) from exc
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


def _pattern(
    validator: Any, pattern: str, instance: Any, _schema: Any
) -> Iterator[ValidationError]:
    """Match ``pattern`` against the whole string, as the registry has always done.

    jsonschema searches, and ``$`` then also matches before a trailing newline.
    """
    if validator.is_type(instance, "string") and not re.fullmatch(pattern, instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


_Validator = validators.extend(Draft202012Validator, {"pattern": _pattern})
_FORMAT_CHECKER = FormatChecker()

# Keywords that carry no rule, and that jsonschema reads as annotations.
_ANNOTATIONS = frozenset(
    {"$schema", "$id", "$comment", "$defs", "title", "description", "default", "examples"}
)
# Keywords that hold a subschema, a map of subschemas, or a list of subschemas.
_SUBSCHEMA = frozenset(
    {"items", "additionalProperties", "contains", "not", "if", "then", "else", "propertyNames"}
)
_SUBSCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "dependentSchemas"})
_SUBSCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})


def _subschemas(key: str, value: Any) -> list[tuple[str, Any]]:
    """Return the (label, subschema) pairs that the keyword ``key`` holds."""
    if key in _SUBSCHEMA_MAPS and isinstance(value, dict):
        return [(f"{key}.{name}", sub) for name, sub in value.items()]
    if key in _SUBSCHEMA_LISTS and isinstance(value, list):
        return [(f"{key}[{index}]", sub) for index, sub in enumerate(value)]
    if key in _SUBSCHEMA:
        return [(key, value)]
    return []


def _unsupported(schema: Any, path: str = "$") -> list[str]:
    """List each keyword or format that the validator would skip without a word."""
    if not isinstance(schema, dict):
        return []
    problems: list[str] = []
    for key, value in schema.items():
        nested = _subschemas(key, value)
        for label, sub in nested:
            problems += _unsupported(sub, f"{path}.{label}")
        if key not in _ANNOTATIONS and key not in _Validator.VALIDATORS:
            problems.append(f"{path}: the keyword {key!r} has no effect")
        elif key == "format" and value not in _FORMAT_CHECKER.checkers:
            problems.append(f"{path}: the format {value!r} has no checker here")
    return problems


def _validator(schema: Mapping[str, Any], schema_name: str) -> Validator:
    """Build the validator of a schema. Refuse a schema that the validator would not enforce."""
    try:
        _Validator.check_schema(schema)
    except SchemaError as exc:
        raise HockeyModelError(
            f"{schema_name}: the schema is not valid JSON Schema: {exc.message}; fix the schema"
        ) from exc
    problems = _unsupported(schema)
    if problems:
        raise HockeyModelError(
            f"{schema_name}: {'; '.join(problems)}; "
            "remove it from the schema or add a check for it in models.py"
        )
    return _Validator(schema, format_checker=_FORMAT_CHECKER)


def _as_json(data: Any, schema_name: str) -> Any:
    """Return ``data`` as plain JSON values. A tuple becomes a list, as it does on disk."""
    try:
        return json.loads(json.dumps(data, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise HockeyModelError(
            f"{schema_name}: the record is not plain JSON data: {exc}; "
            "replace NaN, Infinity, and any value JSON cannot hold with a number, text, or null"
        ) from exc


def validate_against_schema(
    record: ExperimentRecord | Mapping[str, Any],
    schema: Mapping[str, Any],
    schema_name: str = "schema",
) -> None:
    """Validate a record against a JSON Schema (Draft 2020-12) given as a mapping.

    Raises ``HockeyModelError`` for the best-matching error. The message names the JSON path,
    the schema, the failing rule, the fix, and the count of errors it does not show.
    """
    data = record.to_dict() if isinstance(record, ExperimentRecord) else record
    validator = _validator(schema, schema_name)
    errors = list(validator.iter_errors(_as_json(data, schema_name)))
    error = best_match(errors)
    if error is None:
        return
    hidden = len(errors) - 1
    more = f" ({hidden} more error{'s' if hidden > 1 else ''} not shown)" if hidden else ""
    raise HockeyModelError(
        f"{error.json_path} in {schema_name}: {error.message} "
        f"(rule {error.validator!r}); change the value to match the schema{more}"
    )
