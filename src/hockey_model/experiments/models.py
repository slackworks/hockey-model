"""Data models for experiment registry records.

Each record describes one experiment: a hypothesis, the data it uses,
and the criteria for promoting or rejecting it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hockey_model.json_schema import validate_json
from hockey_model.types import HockeyModelError

# Status values a record can hold.
STATUS_REGISTERED = "registered"
STATUS_REJECTED = "rejected"
STATUS_INCONCLUSIVE = "inconclusive"
STATUS_PROMOTED = "promoted"
# A withdrawn record never ran and never will. It keeps its place in the history, spends no look,
# holds no result, and cannot return to another status.
STATUS_WITHDRAWN = "withdrawn"
VALID_STATUSES = (
    STATUS_REGISTERED,
    STATUS_REJECTED,
    STATUS_INCONCLUSIVE,
    STATUS_PROMOTED,
    STATUS_WITHDRAWN,
)
# The statuses that only a result earns, so only a confirmation run may hold one.
RESULT_STATUSES = (STATUS_REJECTED, STATUS_INCONCLUSIVE, STATUS_PROMOTED)

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


def validate_against_schema(
    record: ExperimentRecord | Mapping[str, Any],
    schema: Mapping[str, Any],
    schema_name: str = "schema",
) -> None:
    """Validate a record, or its dict, against a JSON Schema. See ``validate_json``."""
    data = record.to_dict() if isinstance(record, ExperimentRecord) else record
    validate_json(data, schema, schema_name)
