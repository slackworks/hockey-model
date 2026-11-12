"""The publish decision of one report trigger, from the facts the gate script reads.

The gate script reads the schedule (``hockey-model report-due``) and the release (``gh``). It
passes the facts here and prints the answer. ``decide`` returns ``lookup_needed`` when the
answer depends on the release and the caller has not read it yet. Design:
docs/design/daily-report-triggers.md
"""

from dataclasses import dataclass

REASON_DRY_RUN = "dry-run"
REASON_NO_GAMES = "no-games"
REASON_REPLACE = "replace"
REASON_ALREADY_PUBLISHED = "already-published"
REASON_DUE = "due"
REASON_LATE = "late"
REASONS = (
    REASON_DRY_RUN,
    REASON_NO_GAMES,
    REASON_REPLACE,
    REASON_ALREADY_PUBLISHED,
    REASON_DUE,
    REASON_LATE,
)


@dataclass(frozen=True, slots=True)
class GateDecision:
    """What one trigger does. ``lookup_needed`` means the release state decides the rest."""

    due: bool
    publish: bool
    reason: str
    lookup_needed: bool = False


def lookup_needed(*, dry_run: bool, games: int) -> bool:
    """Return True when the answer depends on whether the day has a release."""
    return not dry_run and games > 0


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
