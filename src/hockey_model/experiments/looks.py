"""The promotion bar of a confirmation record, by look number.

Design: docs/design/promotion-bar.md
"""

from collections.abc import Sequence
from fractions import Fraction

from hockey_model.backtest.metrics import BOOTSTRAP_RESAMPLES
from hockey_model.experiments.models import STATUS_WITHDRAWN, ExperimentRecord
from hockey_model.game_model.seasons import INTERVAL_TAIL
from hockey_model.types import HockeyModelError

# All looks together may promote a feature with no effect with at most this chance. The tail of
# look k is BASE_TAIL / (k * (k + 1)), so the tails of the first n looks sum to
# BASE_TAIL * n / (n + 1), which is below BASE_TAIL for every n.
BASE_TAIL = INTERVAL_TAIL
# The tail of the last look leaves at least this many resampled means outside each end.
MIN_TAIL_RESAMPLES = 5
CONFIRMATION = "confirmation"


def _supported_looks() -> int:
    """Return the last look whose tail still leaves ``MIN_TAIL_RESAMPLES`` bootstrap means.

    More resamples in ``BOOTSTRAP_RESAMPLES`` raise the cap.
    """
    look = 1
    while (
        Fraction(BASE_TAIL) / ((look + 1) * (look + 2)) * BOOTSTRAP_RESAMPLES >= MIN_TAIL_RESAMPLES
    ):
        look += 1
    return look


MAX_LOOKS = _supported_looks()


def promotion_tail_exact(look_number: int) -> Fraction:
    """Return the share the interval cuts at look ``look_number``, as an exact fraction.

    The share is ``BASE_TAIL / (look_number * (look_number + 1))``.
    """
    if not 1 <= look_number <= MAX_LOOKS:
        raise HockeyModelError(
            f"look_number {look_number} is outside the supported range 1 to {MAX_LOOKS}; "
            "register a new hypothesis instead of a further look at the same games"
        )
    return Fraction(BASE_TAIL) / (look_number * (look_number + 1))


def promotion_tail(look_number: int) -> float:
    """Return the share the interval cuts at look ``look_number``, as a float."""
    return float(promotion_tail_exact(look_number))


def look_problem(record: ExperimentRecord, earlier: Sequence[ExperimentRecord]) -> str | None:
    """Return why the look number of ``record`` is wrong, or None when it is right.

    Every confirmation record scores the games after its own cutoff, in the same open-ended
    seasons, so records overlap on every game after the later cutoff. The look number of a
    confirmation record is therefore one more than the count of earlier confirmation records.
    A withdrawn record spends no look, because it never ran. ``earlier`` holds the records with
    a lower registry number.
    """
    if record.cohort != CONFIRMATION:
        return None
    spent = [r.id for r in earlier if r.cohort == CONFIRMATION and r.status != STATUS_WITHDRAWN]
    expected = len(spent) + 1
    if record.look_number != expected:
        named = ", ".join(spent) if spent else "none"
        return (
            f"look_number is {record.look_number}, expected {expected}: "
            f"earlier confirmation records score overlapping games ({named}); "
            f"set look_number to {expected}"
        )
    return None
