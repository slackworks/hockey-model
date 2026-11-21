"""The checks that look at the whole registry, not at one record."""

from pathlib import Path

from hockey_model.experiments.history import deleted_record_ids, history_unavailable_reason
from hockey_model.experiments.registry import duplicate_numbers, misnamed_record_files


def registry_errors(ids: list[str], repo_root: Path) -> list[str]:
    """Return the errors that belong to the registry as a whole, not to one record."""
    errors = [
        f"registry number {number} is used by more than one record; a new record needs a new number"
        for number in duplicate_numbers(ids)
    ]
    errors.extend(
        f"{record_id}: tracked in the repository history but missing from the working tree; "
        "a registered record cannot be deleted. To drop a record that never ran, set its "
        "status to 'withdrawn' instead"
        for record_id in deleted_record_ids(ids, repo_root)
    )
    errors.extend(
        f"{name}: the registry holds this JSON file, but its name is not a record id, so the "
        "look count skips it; rename it to NNNN-slug.json or remove it"
        for name in misnamed_record_files(repo_root)
    )
    return errors


def registry_notes(repo_root: Path) -> list[str]:
    """Return the checks that did not run, so a quiet output is not read as a pass."""
    reason = history_unavailable_reason(repo_root)
    if reason is None:
        return []
    return [f"deletion history not checked: {reason}"]
