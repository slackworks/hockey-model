"""Experiment registry: list and load records.

Records live at ``experiments/NNNN-<slug>.json`` in the repository.
Records are written by hand. The checker validates them and enforces the registry rules.
"""

from __future__ import annotations

import re
from pathlib import Path

from hockey_model.experiments.models import ExperimentRecord
from hockey_model.experiments.schemas import RECORD_ID_PATTERN
from hockey_model.types import HockeyModelError

# The registry is repository data, not package data, so every call names the
# repository root it reads from. The schema ships with the package, so validation
# does not depend on the working directory.
REGISTRY_DIR_NAME = "experiments"
# Per-game CSVs of a confirmation run live here so the record's hash points at a
# file other people can read.
RESULTS_DIR_NAME = "results"

# The number of a record id is the record's slot in the registry, so two records cannot share it.
RECORD_STEM = re.compile(RECORD_ID_PATTERN)


def registry_dir(repo_root: Path) -> Path:
    """Return the registry directory inside the repository root."""
    return repo_root / REGISTRY_DIR_NAME


def results_dir(repo_root: Path) -> Path:
    """Return the directory that holds the per-game CSVs of a confirmation run."""
    return registry_dir(repo_root) / RESULTS_DIR_NAME


def list_records(repo_root: Path) -> list[str]:
    """Return all record IDs in the registry, sorted."""
    registry = registry_dir(repo_root)
    if not registry.exists():
        return []
    ids: list[str] = []
    for path in sorted(registry.iterdir()):
        if path.suffix == ".json" and RECORD_STEM.fullmatch(path.stem):
            ids.append(path.stem)
    return sorted(ids)


def get_record(record_id: str, repo_root: Path) -> ExperimentRecord:
    """Load a record by ID. Raises when the ID is malformed or the record is absent."""
    check_record_id(record_id)
    if not record_exists(record_id, repo_root):
        raise HockeyModelError(f"experiment {record_id!r} not found")
    return ExperimentRecord.load(registry_dir(repo_root) / f"{record_id}.json")


def record_exists(record_id: str, repo_root: Path) -> bool:
    """Return True when a record file for the ID is in the registry."""
    check_record_id(record_id)
    return (registry_dir(repo_root) / f"{record_id}.json").exists()


def check_record_id(record_id: str) -> None:
    """Raise unless the ID has the NNNN-slug shape, so it cannot name an outside path."""
    if not RECORD_STEM.fullmatch(record_id):
        raise HockeyModelError(
            f"invalid experiment id {record_id!r}: use NNNN-slug, for example 0002-attempt-form"
        )


def duplicate_numbers(record_ids: list[str]) -> list[str]:
    """Return the registry numbers that more than one record ID uses, sorted."""
    seen: set[str] = set()
    repeated: set[str] = set()
    for record_id in record_ids:
        number = record_id.split("-", 1)[0]
        (repeated if number in seen else seen).add(number)
    return sorted(repeated)
