"""The promotion bar of a confirmation record, by look number.

Design: docs/design/promotion-bar.md
"""

from collections.abc import Sequence

from hockey_model.experiments.models import ExperimentRecord
from hockey_model.game_model.seasons import INTERVAL_TAIL
from hockey_model.types import HockeyModelError

# The first look cuts this share of the bootstrap at the promoted end of the interval.
BASE_TAIL = INTERVAL_TAIL
# The bootstrap holds BOOTSTRAP_RESAMPLES means, so a smaller tail than BASE_TAIL / MAX_LOOKS
# would rest on too few resamples.
MAX_LOOKS = 10
CONFIRMATION = "confirmation"


def promotion_tail(look_number: int) -> float:
    """Return the share the interval cuts at look ``look_number``: ``BASE_TAIL / look_number``."""
    if not 1 <= look_number <= MAX_LOOKS:
        raise HockeyModelError(
            f"look_number {look_number} is outside the supported range 1 to {MAX_LOOKS}; "
            "register a new hypothesis instead of a further look at the same games"
        )
    return BASE_TAIL / look_number


def look_problem(record: ExperimentRecord, earlier: Sequence[ExperimentRecord]) -> str | None:
    """Return why the look number of ``record`` is wrong, or None when it is right.

    Every confirmation record scores the games after its own cutoff, in the same open-ended
    seasons, so records overlap on every game after the later cutoff. The look number of a
    confirmation record is therefore one more than the count of earlier confirmation records.
    ``earlier`` holds the records with a lower registry number.
    """
    if record.cohort != CONFIRMATION:
        return None
    spent = [r.id for r in earlier if r.cohort == CONFIRMATION]
    expected = len(spent) + 1
    if record.look_number != expected:
        named = ", ".join(spent) if spent else "none"
        return (
            f"look_number is {record.look_number}, expected {expected}: "
            f"earlier confirmation records score overlapping games ({named}); "
            f"set look_number to {expected}"
        )
    return None
