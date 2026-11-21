"""The model-uncertainty interval of a home win chance (#212).

The interval comes from the uncertainty of the fitted coefficients, by the delta method on the
log-odds scale. It does not hold the noise of the Elo rating, the noise of the feature values,
or the drift between seasons, so a reader must call it "model uncertainty only".
Design: docs/design/win-interval.md
"""

import math
from statistics import NormalDist

from hockey_model.game_model.features import FeatureRow
from hockey_model.game_model.logistic import LogisticFit, linear_score, sigmoid

_ROUNDING = 1e-12


def home_win_interval(
    fit: LogisticFit, row: FeatureRow, level: float
) -> tuple[float, float] | None:
    """Return the ``(low, high)`` home win chance at ``level``, or ``None`` without a covariance.

    The covariance is the inverse Hessian of the penalized loss in the scaled space of the fit,
    so the design row is scaled with the saved means and scales before the variance is read.
    """
    diagnostics = fit.diagnostics
    if diagnostics is None or diagnostics.covariance is None:
        return None
    names = ("elo_logit", *fit.features)
    raw = [row.elo_logit, *(row.values[name] for name in fit.features)]
    design = [1.0]
    design += [
        (value - diagnostics.means[name]) / diagnostics.scales[name]
        for name, value in zip(names, raw, strict=True)
    ]
    sigma = diagnostics.covariance
    variance = sum(
        design[i] * sigma[i][j] * design[j] for i in range(len(design)) for j in range(len(design))
    )
    # The loader checks the matrix, so a negative variance here is more than rounding noise.
    if variance < -_ROUNDING:
        raise ValueError(
            f"win interval: the saved covariance gives a variance of {variance:.3g}, below zero; "
            "run `hockey-model fit` again"
        )
    spread = NormalDist().inv_cdf((1.0 + level) / 2.0) * math.sqrt(max(variance, 0.0))
    score = linear_score(fit, row)
    return sigmoid(score - spread), sigmoid(score + spread)
