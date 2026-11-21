"""A small L2-regularized logistic regression, fitted by Newton-Raphson (IRLS).

It uses the standard library only. The matrices have at most a few columns, so plain
lists are enough.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hockey_model.game_model.features import FeatureRow

L2 = 1.0
MAX_ITERATIONS = 50
# The fit stops when no gradient entry of the penalized loss exceeds this. A step-size test
# is not enough: a clipped step is zero while the gradient is still large.
GRADIENT_TOLERANCE = 1e-6
STOP_REASON = f"converged: largest gradient entry below {GRADIENT_TOLERANCE:g}"
_WEIGHT_CAP = 30.0
# A tiny ridge on the intercept keeps the system solvable when every game has one result.
_INTERCEPT_RIDGE = 1e-6


@dataclass(frozen=True, slots=True)
class FitDiagnostics:
    """What a finished fit shows about itself, in the scaled space where the penalty acts.

    ``condition`` is the largest over the smallest pivot of the Hessian at the solution.
    ``means`` and ``scales`` map each column name (``elo_logit`` first) to its scaling.
    ``covariance`` is the inverse of that Hessian, with the intercept first and then the columns
    in the order of ``means``. A fit saved before the covariance existed has ``None``.
    """

    iterations: int
    objective: float
    gradient_norm: float
    condition: float
    l2: float
    means: Mapping[str, float]
    scales: Mapping[str, float]
    stop_reason: str
    covariance: tuple[tuple[float, ...], ...] | None = None


@dataclass(frozen=True, slots=True)
class LogisticFit:
    """Weights for ``intercept + elo_weight * elo_logit + sum(weights[f] * value[f])``."""

    features: tuple[str, ...]
    intercept: float
    elo_weight: float
    weights: Mapping[str, float]
    diagnostics: FitDiagnostics | None = None


ELO_ONLY = LogisticFit(features=(), intercept=0.0, elo_weight=1.0, weights={})


def sigmoid(z: float) -> float:
    """Return the logistic function of ``z`` without overflow."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp = math.exp(z)
    return exp / (1.0 + exp)


def solve_linear(matrix: list[list[float]], vector: list[float]) -> tuple[list[float], float]:
    """Solve ``matrix @ x = vector`` by Gaussian elimination with partial pivoting.

    Also return the largest over the smallest pivot, a cheap measure of conditioning.
    """
    size = len(vector)
    rows = [[*matrix[i], vector[i]] for i in range(size)]
    pivots = []
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:  # noqa: PLR2004  # the L2 term keeps this positive
            raise ValueError("fit logistic: the normal equations are singular")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        pivots.append(abs(rows[col][col]))
        for r in range(col + 1, size):
            factor = rows[r][col] / rows[col][col]
            for c in range(col, size + 1):
                rows[r][c] -= factor * rows[col][c]
    solution = [0.0] * size
    for r in range(size - 1, -1, -1):
        tail = sum(rows[r][c] * solution[c] for c in range(r + 1, size))
        solution[r] = (rows[r][size] - tail) / rows[r][r]
    return solution, max(pivots) / min(pivots)


def _design(rows: Sequence[FeatureRow], features: Sequence[str]) -> list[list[float]]:
    return [[row.elo_logit, *(row.values[f] for f in features)] for row in rows]


def _standardize(
    data: list[list[float]],
) -> tuple[list[list[float]], list[float], list[float]]:
    count = len(data)
    columns = len(data[0])
    means = [sum(row[c] for row in data) / count for c in range(columns)]
    scales = []
    for c in range(columns):
        variance = sum((row[c] - means[c]) ** 2 for row in data) / count
        scales.append(math.sqrt(variance) if variance > 0 else 1.0)
    scaled = [[(row[c] - means[c]) / scales[c] for c in range(columns)] for row in data]
    return scaled, means, scales


def _softplus(z: float) -> float:
    return max(z, 0.0) + math.log1p(math.exp(-abs(z)))


def _system(
    data: list[list[float]], wins: Sequence[bool], beta: list[float]
) -> tuple[list[float], list[list[float]], float]:
    """Return the gradient, the Hessian, and the value of the penalized loss at ``beta``."""
    size = len(beta)
    penalty = [_INTERCEPT_RIDGE] + [L2] * (size - 1)
    gradient = [penalty[j] * beta[j] for j in range(size)]
    hessian = [[penalty[i] if i == j else 0.0 for j in range(size)] for i in range(size)]
    objective = 0.5 * sum(penalty[j] * beta[j] ** 2 for j in range(size))
    for row, win in zip(data, wins, strict=True):
        x = [1.0, *row]
        z = sum(b * v for b, v in zip(beta, x, strict=True))
        p = sigmoid(z)
        weight = p * (1.0 - p)
        objective += _softplus(z) - (z if win else 0.0)
        for i in range(size):
            gradient[i] += (p - (1.0 if win else 0.0)) * x[i]
            for j in range(size):
                hessian[i][j] += weight * x[i] * x[j]
    return gradient, hessian, objective


@dataclass(frozen=True, slots=True)
class _Solution:
    beta: list[float]
    iterations: int
    objective: float
    gradient_norm: float
    condition: float
    covariance: tuple[tuple[float, ...], ...]


def _inverse(matrix: list[list[float]]) -> tuple[tuple[float, ...], ...]:
    """Return the inverse of a symmetric matrix, one unit vector at a time."""
    size = len(matrix)
    columns = [
        solve_linear(matrix, [1.0 if r == c else 0.0 for r in range(size)])[0] for c in range(size)
    ]
    return tuple(tuple(columns[c][r] for c in range(size)) for r in range(size))


def _newton(data: list[list[float]], wins: Sequence[bool]) -> _Solution:
    """Run Newton steps until the gradient of the penalized loss is below its tolerance."""
    beta = [0.0] * (len(data[0]) + 1)
    iteration = 0
    while True:
        gradient, hessian, objective = _system(data, wins, beta)
        norm = max(abs(g) for g in gradient)
        step, condition = solve_linear(hessian, gradient)
        if norm < GRADIENT_TOLERANCE:
            return _Solution(beta, iteration, objective, norm, condition, _inverse(hessian))
        if iteration == MAX_ITERATIONS:
            raise ValueError(
                f"fit logistic: no convergence in {MAX_ITERATIONS} iterations; the largest "
                f"gradient entry is {norm:.3g}, above {GRADIENT_TOLERANCE:g}; "
                "check the feature rows"
            )
        beta = [max(-_WEIGHT_CAP, min(_WEIGHT_CAP, b - d)) for b, d in zip(beta, step, strict=True)]
        iteration += 1


def fit_logistic(
    rows: Sequence[FeatureRow], wins: Sequence[bool], features: Sequence[str]
) -> LogisticFit:
    """Fit the home win on the Elo logit and ``features``.

    Features are scaled to unit variance inside the fit, so ``L2`` treats them alike.
    The returned weights are on the original scale. The diagnostics are on the scaled one.
    A fit whose gradient is not below ``GRADIENT_TOLERANCE`` raises instead of returning.
    """
    if not rows:
        raise ValueError("fit logistic: no rows; build the feature rows first")
    if len(rows) != len(wins):
        raise ValueError(f"fit logistic: {len(rows)} rows but {len(wins)} results; pass one each")
    data, means, scales = _standardize(_design(rows, features))
    solved = _newton(data, wins)
    beta = solved.beta
    raw = [beta[c + 1] / scales[c] for c in range(len(scales))]
    intercept = beta[0] - sum(raw[c] * means[c] for c in range(len(scales)))
    names = ("elo_logit", *features)
    diagnostics = FitDiagnostics(
        iterations=solved.iterations,
        objective=solved.objective,
        gradient_norm=solved.gradient_norm,
        condition=solved.condition,
        l2=L2,
        means=dict(zip(names, means, strict=True)),
        scales=dict(zip(names, scales, strict=True)),
        stop_reason=STOP_REASON,
        covariance=solved.covariance,
    )
    return LogisticFit(
        tuple(features), intercept, raw[0], dict(zip(features, raw[1:], strict=True)), diagnostics
    )


def linear_score(fit: LogisticFit, row: FeatureRow) -> float:
    """Return the log odds of a home win for one feature row."""
    z = fit.intercept + fit.elo_weight * row.elo_logit
    return z + sum(fit.weights[f] * row.values[f] for f in fit.features)


def predict_logistic(fit: LogisticFit, row: FeatureRow) -> float:
    """Return the home win chance for one feature row."""
    return sigmoid(linear_score(fit, row))
