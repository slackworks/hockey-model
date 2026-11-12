"""The JSON Schemas used by the experiment registry.

The schemas are Python constants, not files on disk. The checker then works
whatever the working directory is, and a test does not need a copy of the
registry directory. Records still live in ``experiments/`` in the repository.

The validator in ``models.py`` supports only the keywords listed in
``_SCHEMA_KEYWORDS``, so these schemas use no other keyword.
"""

from __future__ import annotations

from typing import Any

from hockey_model.experiments.looks import MAX_LOOKS

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
        "id": {"type": "string", "pattern": "^[0-9]{4}-[a-z0-9][a-z0-9-]*$"},
        "title": {"type": "string"},
        "hypothesis": {"type": "string"},
        "cohort": {"type": "string", "enum": ["exploration", "confirmation"]},
        "data_cutoff": {"type": "string", "format": "date"},
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
            "enum": ["registered", "rejected", "inconclusive", "promoted"],
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
    "required": ["ok", "records", "errors"],
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
    },
}
