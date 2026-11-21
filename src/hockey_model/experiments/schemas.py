"""The JSON Schemas used by the experiment registry.

The schemas are Python constants, not files on disk. The checker then works
whatever the working directory is, and a test does not need a copy of the
registry directory. Records still live in ``experiments/`` in the repository.

The schemas follow JSON Schema Draft 2020-12, and ``models.py`` checks them with jsonschema.
The validator matches a ``pattern`` against the whole string and refuses a keyword or a format
that it would skip.
"""

from __future__ import annotations

from typing import Any

from hockey_model.experiments.looks import MAX_LOOKS
from hockey_model.experiments.models import VALID_STATUSES

# A record id is a four-digit number and a slug. The number is the record's slot in the registry.
RECORD_ID_PATTERN = "^[0-9]{4}-[a-z0-9][a-z0-9-]*$"
# fromisoformat also reads 20261031 and 2026-W44-6, which are not the registered form.
DATE_PATTERN = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"

EXPERIMENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "id",
        "title",
        "hypothesis",
        "cohort",
        "data_cutoff",
        "primary_metric",
        "baseline",
        "min_games",
        "candidate_features",
        "look_number",
        "promotion_criterion",
        "failure_outcome",
        "status",
    ],
    "properties": {
        "id": {"type": "string", "pattern": RECORD_ID_PATTERN},
        "title": {"type": "string"},
        "hypothesis": {"type": "string"},
        "cohort": {"type": "string", "enum": ["exploration", "confirmation"]},
        "data_cutoff": {
            "type": "string",
            "format": "date",
            "pattern": DATE_PATTERN,
        },
        "primary_metric": {"type": "string"},
        "baseline": {
            "type": "object",
            "required": ["fit_id", "commit"],
            "properties": {"fit_id": {"type": "integer"}, "commit": {"type": "string"}},
        },
        "selection_folds": {
            "type": ["array", "null"],
            "items": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
        },
        "min_games": {"type": "integer", "minimum": 1},
        "candidate_features": {"type": "array", "items": {"type": "string"}},
        "look_number": {"type": "integer", "minimum": 1, "maximum": MAX_LOOKS},
        "expected_effect": {"type": ["number", "null"]},
        "promotion_criterion": {"type": "string"},
        "failure_outcome": {"type": "string"},
        "status": {
            "type": "string",
            "enum": list(VALID_STATUSES),
        },
        "result": {
            "type": ["object", "null"],
            "properties": {
                "games": {"type": "integer", "minimum": 0},
                "difference": {"type": "number"},
                "interval": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "csv_path": {"type": "string"},
                "csv_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
    },
}

CHECK_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["ok", "records", "errors", "notes"],
    "properties": {
        "ok": {"type": "boolean"},
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "valid"],
                "properties": {
                    "id": {"type": "string"},
                    "valid": {"type": "boolean"},
                    "status": {"type": "string"},
                    "error": {"type": "string"},
                    "has_result": {"type": "boolean"},
                    "registration_commit": {"type": "string"},
                    "immutability": {"type": "string"},
                },
            },
        },
        "errors": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}
