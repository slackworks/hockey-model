"""Scores for win probabilities, and a block bootstrap for their differences."""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

# A probability is held inside [PROB_FLOOR, 1 - PROB_FLOOR] before a log, so one extreme
# miss gives a large finite loss, not infinity.
PROB_FLOOR = 1e-6
CALIBRATION_BINS = 10
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 0
# Games of one night share goalies and teams, so a block is one night's slate. The busiest
# NHL night has 16 games. The lag-1 autocorrelation of a per-game loss difference is near
# zero, so a longer block would add code and no width.
MIN_BLOCK_LENGTH = 16
# A resample of fewer independent blocks than this repeats a few blocks, so its interval is
# narrow for lack of data, not for lack of risk. A series this short has no usable interval.
MIN_INDEPENDENT_BLOCKS = 10


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """The games whose predicted chance falls in one bin."""

    low: float
    high: float
    count: int
    mean_prob: float | None
    win_rate: float | None


def _check(probs: Sequence[float], wins: Sequence[bool]) -> None:
    if not probs:
        raise ValueError("score probabilities: the list is empty; pass at least one game")
    if len(probs) != len(wins):
        raise ValueError(
            f"score probabilities: {len(probs)} probabilities but {len(wins)} results; "
            "pass one each"
        )
    for value in probs:
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"score probabilities: {value!r} is not a probability in [0, 1]")


def per_game_log_loss(probs: Sequence[float], wins: Sequence[bool]) -> list[float]:
    """Return the log loss of each game."""
    _check(probs, wins)
    losses = []
    for value, win in zip(probs, wins, strict=True):
        clamped = min(max(value, PROB_FLOOR), 1.0 - PROB_FLOOR)
        losses.append(-math.log(clamped if win else 1.0 - clamped))
    return losses


def log_loss(probs: Sequence[float], wins: Sequence[bool]) -> float:
    """Return the mean log loss. Lower is better."""
    losses = per_game_log_loss(probs, wins)
    return sum(losses) / len(losses)


def brier(probs: Sequence[float], wins: Sequence[bool]) -> float:
    """Return the mean squared error of the probabilities. Lower is better."""
    _check(probs, wins)
    return sum((p - (1.0 if w else 0.0)) ** 2 for p, w in zip(probs, wins, strict=True)) / len(
        probs
    )


def calibration(probs: Sequence[float], wins: Sequence[bool]) -> tuple[CalibrationBin, ...]:
    """Return ten equal bins of predicted chance, with the share of games won in each."""
    _check(probs, wins)
    members: list[list[tuple[float, bool]]] = [[] for _ in range(CALIBRATION_BINS)]
    for value, win in zip(probs, wins, strict=True):
        members[min(int(value * CALIBRATION_BINS), CALIBRATION_BINS - 1)].append((value, win))
    result = []
    for index, games in enumerate(members):
        low, high = index / CALIBRATION_BINS, (index + 1) / CALIBRATION_BINS
        if games:
            mean_prob = sum(p for p, _ in games) / len(games)
            win_rate = sum(w for _, w in games) / len(games)
            result.append(CalibrationBin(low, high, len(games), mean_prob, win_rate))
        else:
            result.append(CalibrationBin(low, high, 0, None, None))
    return tuple(result)


def independent_blocks(count: int) -> int:
    """Return how many whole blocks of ``MIN_BLOCK_LENGTH`` games ``count`` games hold."""
    return count // MIN_BLOCK_LENGTH


def bootstrap_problem(count: int) -> str | None:
    """Return why ``count`` games are too few for a bootstrap interval, or ``None``."""
    blocks = independent_blocks(count)
    if blocks >= MIN_INDEPENDENT_BLOCKS:
        return None
    return (
        f"{count} scored games hold {blocks} independent blocks of {MIN_BLOCK_LENGTH}; "
        f"the bootstrap needs at least {MIN_INDEPENDENT_BLOCKS}"
    )


def _cut(tail: float, count: int) -> int:
    """Return how many means each end leaves outside, or raise when that names no interval."""
    cut = round(tail * count) if math.isfinite(tail) else 0
    if not 1 <= cut < count / 2:
        raise ValueError(
            f"bootstrap the mean: tail {tail} cuts {cut} of {count} resamples from each end; "
            "pass a tail that cuts at least one and fewer than half"
        )
    return cut


def interval_ends(sorted_means: Sequence[float], tail: float) -> tuple[float, float]:
    """Return the ends of the interval that leaves ``round(tail * N)`` means outside each end."""
    cut = _cut(tail, len(sorted_means))
    return sorted_means[cut], sorted_means[len(sorted_means) - 1 - cut]


def block_bootstrap_mean_ci(values: Sequence[float], tail: float) -> tuple[float, float]:
    """Return the ``tail`` and ``1 - tail`` percentile of the mean over block resamples.

    Each end leaves the same count of resamples outside the interval. A ``tail`` that cuts no
    resample, or half or more of them, raises, because it names no interval.

    ``values`` must be in game start order. Blocks are runs of consecutive games, because
    games on one night share teams and goalies. A list with fewer than
    ``MIN_INDEPENDENT_BLOCKS`` blocks raises, because its resamples would repeat one or two
    blocks and show a false certainty.
    """
    _cut(tail, BOOTSTRAP_RESAMPLES)
    if not values:
        raise ValueError("bootstrap the mean: the list is empty; pass at least one value")
    problem = bootstrap_problem(len(values))
    if problem is not None:
        raise ValueError(f"bootstrap the mean: {problem}; score more games")
    if not all(math.isfinite(v) for v in values):
        raise ValueError("bootstrap the mean: a value is not finite; check the scores")
    count = len(values)
    # One night's slate, and no more than the whole series.
    length = min(count, MIN_BLOCK_LENGTH)
    rng = random.Random(BOOTSTRAP_SEED)
    means = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        total = 0.0
        taken = 0
        while taken < count:
            start = rng.randrange(count - length + 1)
            piece = values[start : start + length][: count - taken]
            total += sum(piece)
            taken += len(piece)
        means.append(total / count)
    means.sort()
    return interval_ends(means, tail)
