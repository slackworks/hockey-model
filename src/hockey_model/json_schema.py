"""Validate JSON data against a JSON Schema (Draft 2020-12) with the jsonschema library.

The registry records and the published daily report share this one validator. It differs from
plain jsonschema in three ways: a ``pattern`` must match the whole string, a schema that holds
a keyword or a format that nothing enforces is refused, and data that is not plain JSON
(NaN, Infinity, a date) is refused with a message that names the fix.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import SchemaError, ValidationError, best_match
from jsonschema.protocols import Validator

from hockey_model.types import HockeyModelError


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


def validate_json(
    data: Any,
    schema: Mapping[str, Any],
    schema_name: str = "schema",
) -> None:
    """Validate JSON data against a JSON Schema (Draft 2020-12) given as a mapping.

    Raises ``HockeyModelError`` for the best-matching error. The message names the JSON path,
    the schema, the failing rule, the fix, and the count of errors it does not show.
    A schema that is not valid, or that holds a keyword or format nothing enforces, is refused.
    """
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
