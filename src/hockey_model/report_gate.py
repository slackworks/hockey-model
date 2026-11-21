"""The publish decision of one report trigger, from the facts the gate script reads.

The gate script reads the schedule (``hockey-model report-due``) and the release (``gh``). It
passes the facts here and prints the answer. ``decide`` returns ``lookup_needed`` when the
answer depends on the release and the caller has not read it yet. Design:
docs/design/daily-report-triggers.md
"""

import json
from dataclasses import dataclass
from pathlib import Path

from hockey_model.types import HockeyModelError

REASON_DRY_RUN = "dry-run"
REASON_NO_GAMES = "no-games"
REASON_REPLACE = "replace"
REASON_UNCHANGED = "unchanged"
REASON_ALREADY_PUBLISHED = "already-published"
REASON_DUE = "due"
REASON_LATE = "late"


@dataclass(frozen=True, slots=True)
class GateDecision:
    """What one trigger does. ``lookup_needed`` means the release state decides the rest."""

    due: bool
    publish: bool
    reason: str
    lookup_needed: bool = False


def decide(
    *, due: bool, force: bool, dry_run: bool, games: int, published: bool | None
) -> GateDecision:
    """Return the decision for one trigger.

    ``due`` says this run beats the first puck drop, or is forced. ``published`` says the day
    has a release, and is None until the caller reads it. A dry run builds a report and never
    publishes it, and it never needs the release. A day with no games publishes nothing.
    """
    if dry_run:
        return GateDecision(due, False, REASON_DRY_RUN)
    if games == 0:
        return GateDecision(False, False, REASON_NO_GAMES)
    if published is None:
        # The release state decides the answer, and the caller has not read it yet.
        return GateDecision(due, False, REASON_DUE, lookup_needed=True)
    if published and due and not force:
        return GateDecision(True, True, REASON_REPLACE)
    if published:
        return GateDecision(False, False, REASON_ALREADY_PUBLISHED)
    if due:
        return GateDecision(True, True, REASON_DUE)
    # The day has no release, and this run cannot beat the puck drop. The games that have not
    # started still need a report, so the first report of a day always publishes.
    return GateDecision(True, True, REASON_LATE)


def warning(
    decision: GateDecision, *, day: str, first_drop: str, margin_seconds: int
) -> str | None:
    """Return the GitHub Actions warning for a decision, or None when it needs none."""
    if decision.reason == REASON_NO_GAMES:
        return (
            f"::warning::report-gate: the schedule lists no games for {day}, "
            "so no report is published."
        )
    if decision.reason == REASON_LATE:
        at = f" at {first_drop}" if first_drop else ""
        return (
            f"::warning::report-gate: the first puck drop of {day}{at} has passed or is less "
            f"than {margin_seconds} seconds away. This run publishes the first report of the "
            "day late."
        )
    return None


def _read_report(path: Path, role: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HockeyModelError(
            f"report-compare: cannot read the {role} report {path}: {error}; "
            "pass the path of a report JSON file"
        ) from error
    if not isinstance(payload, dict):
        raise HockeyModelError(
            f"report-compare: the {role} report {path} is not a JSON object; "
            "pass the path of a report JSON file"
        )
    return payload


def compare_inputs(built_path: Path, published_path: Path) -> dict[str, object]:
    """Say whether the built report has the same inputs as the newest published report.

    The built report MUST carry ``inputs_sha256``. A published report that has none, such as
    a build from before the field existed, counts as changed. The answer has ``reason``
    ``unchanged`` when both hashes are equal, and ``replace`` otherwise. ``baseline`` says
    whether the published report had a hash to compare.
    """
    built = _read_report(built_path, "built").get("inputs_sha256")
    if not isinstance(built, str) or not built:
        raise HockeyModelError(
            f"report-compare: the built report {built_path} has no inputs_sha256 string; "
            "build it with the current code"
        )
    published = _read_report(published_path, "published").get("inputs_sha256")
    baseline = isinstance(published, str) and bool(published)
    unchanged = baseline and published == built
    return {
        "unchanged": unchanged,
        "baseline": baseline,
        "reason": REASON_UNCHANGED if unchanged else REASON_REPLACE,
    }
