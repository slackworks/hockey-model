"""The share of fit rows where a selected feature was imputed, and the limit that refuses a fit.

A row counts for a feature once, however many reasons it has. A measured zero is not imputed.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from hockey_model.game_model.features import FEATURES, FeatureRow
from hockey_model.types import HockeyModelError

# The largest share of rows that may use a stand-in for one selected feature. It was set
# before any experiment ran with it, and it is not tuned to a result. See #66.
IMPUTATION_LIMIT = 0.10


class ImputationLimitError(HockeyModelError):
    """A selected feature is imputed in more rows than the limit allows."""


@dataclass(frozen=True, slots=True)
class FeatureQuality:
    """How often one selected feature was imputed across a set of rows."""

    feature: str
    imputed: int
    total: int
    share: float


def validate_limit(limit: float) -> float:
    """Return ``limit`` when it is a share above zero and at most one. Raise otherwise."""
    if not (math.isfinite(limit) and 0.0 < limit <= 1.0):
        raise ValueError(f"imputation limit {limit!r} must be a share above 0 and at most 1")
    return limit


def feature_quality(rows: Sequence[FeatureRow], names: Sequence[str]) -> tuple[FeatureQuality, ...]:
    """Return the imputed count and share of each selected feature in ``rows``.

    An empty ``rows`` raises. A share over no rows is not a zero share. A name that no row can
    impute raises, so a feature with no imputed path cannot pass the limit unseen.
    """
    unknown = [name for name in names if name not in FEATURES]
    if unknown:
        raise ValueError(f"imputation share: {unknown[0]!r} is not a known feature")
    if not rows:
        raise ValueError("imputation share: no rows; an empty denominator has no share")
    counts = dict.fromkeys(names, 0)
    for row in rows:
        for feature in {d.feature for d in row.imputed} & counts.keys():
            counts[feature] += 1
    total = len(rows)
    return tuple(FeatureQuality(n, counts[n], total, counts[n] / total) for n in names)


def check_imputation(
    rows: Sequence[FeatureRow], names: Sequence[str], limit: float = IMPUTATION_LIMIT
) -> tuple[FeatureQuality, ...]:
    """Return the quality of each selected feature. Raise when one is above ``limit``.

    The error names each feature over the limit and the change that fixes it.
    """
    validate_limit(limit)
    quality = feature_quality(rows, names)
    over = [q for q in quality if q.share > limit]
    if over:
        named = "; ".join(
            f"{q.feature} is imputed in {q.imputed} of {q.total} rows ({q.share:.1%})" for q in over
        )
        raise ImputationLimitError(
            f"feature refused: {named}, above the limit of {limit:.1%}. "
            "Fill the missing source games, or drop the feature from the fit or the candidates."
        )
    return quality
