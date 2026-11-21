"""The withdrawn status of an experiment record: its history rules and its results-file rule.

A withdrawn record never ran and never will. It keeps its place in the registry history, spends
no look, and holds no result. Design: docs/design/promotion-bar.md
"""

from collections.abc import Sequence
from pathlib import Path

from hockey_model.experiments.history import field_history, path_in_history
from hockey_model.experiments.models import STATUS_REGISTERED, STATUS_WITHDRAWN, ExperimentRecord
from hockey_model.experiments.registry import REGISTRY_DIR_NAME, RESULTS_DIR_NAME
from hockey_model.types import HockeyModelError


def status_problem(history: Sequence[tuple[str, object]], head: str) -> str | None:
    """Return why the status ``head`` breaks the withdrawn rules, or None when it does not.

    ``history`` holds the ``(commit, status)`` pairs of the earlier commits of the record.
    A withdrawn record is final, so no later status may differ. Only a registered record may
    be withdrawn: any other earlier status means a run happened and spent the look.
    """
    for commit, status in history:
        if status == STATUS_WITHDRAWN and head != STATUS_WITHDRAWN:
            return (
                f"commit {commit} recorded the status 'withdrawn', so the record cannot "
                f"return to {head!r}; register a new record"
            )
        if head == STATUS_WITHDRAWN and status not in {STATUS_REGISTERED, STATUS_WITHDRAWN}:
            return (
                f"commit {commit} recorded the status {status!r}, so a run may have seen the "
                "games and spent the look; only a registered record can be withdrawn"
            )
    return None


def check_withdrawn(record: ExperimentRecord, repo_root: Path) -> None:
    """Raise when the record breaks the withdrawn rules in its history or in its files.

    A withdrawn record has no per-game results file, in the working tree or in any commit,
    because a run writes that file and so shows that the games were seen.
    """
    problem = status_problem(field_history(record.id, "status", repo_root), record.status)
    if problem is not None:
        raise HockeyModelError(f"experiment {record.id!r}: {problem}")
    if record.status != STATUS_WITHDRAWN:
        return
    relative = f"{REGISTRY_DIR_NAME}/{RESULTS_DIR_NAME}/{record.id}-games.csv"
    if (repo_root / relative).exists() or path_in_history(relative, repo_root):
        raise HockeyModelError(
            f"experiment {record.id!r}: status is 'withdrawn' but {relative} exists or did "
            "exist; a run writes that file, so the games were seen and the look is spent"
        )
