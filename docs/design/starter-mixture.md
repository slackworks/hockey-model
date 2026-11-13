# Unknown starter as a mixture (#49)

Uses H1, H3 to H5, H8 of [research-harness.md](research-harness.md).

## Goal

When no source confirms a starter, predict with each likely goalie, weighted by the chance that each one starts.
Average the final probabilities, not the goalie ratings.
A confirmed starter gives exactly today's prediction.

## What exists

`resolve_goalie` in `game_model/goalies.py` picks one goalie: a goalie given on the game, the boxscore starter after puck drop, the earliest confirmed read, the newest likely read, then the goalie with the most starts in the last `STARTER_WINDOW` team games (`_guess`), with injured and departed goalies left out.

The mixture changes a prediction only when the fit uses a goalie feature. The live fit (`xg_form`, `rest`) does not, so this issue matters once #45 or #46 passes. Build the candidate model now, and score it on its own.

## Candidate model

New module `game_model/starter_odds.py`:

```python
@dataclass(frozen=True, slots=True)
class StarterCandidate:
    start: GoalieStart  # status UNKNOWN or LIKELY, source MODEL_GUESS or the read
    probability: float


def starter_candidates(
    conn, game: Game, team: str, as_of: datetime, *, use_reads: bool, catalog: Catalog
) -> tuple[tuple[StarterCandidate, ...], tuple[MissingInput, ...]]: ...
```

Rules, in order:

1. A goalie given on the game, the boxscore starter after puck drop (when allowed), or a confirmed read: one candidate with probability 1.
2. The candidate pool: goalies with a start in the last `STARTER_WINDOW` team games, minus the injured and departed ones (the same filters as `_guess`). A goalie named by a likely read joins the pool even with no recent start.
3. Weights. For each candidate `i`: `score_i = log(starts_i + ALPHA) + BETA * started_last_game_i * back_to_back + GAMMA * likely_read_i`. `back_to_back` is 1 when the team played the day before. The probabilities are `softmax(score)`.
4. An empty pool gives no candidate and the existing "no starter" note.

Fit `ALPHA`, `BETA`, and `GAMMA` by maximum likelihood of the actual boxscore starter with `scipy.optimize.minimize`, on the `SELECT_TRAIN` seasons.
History has no likely reads before the store kept them. So fit `ALPHA` and `BETA` on all `SELECT_TRAIN` games with `GAMMA = 0`, then fit `GAMMA` alone on the games that have a stored likely read. When fewer than 300 such games exist, set `GAMMA` from the share of likely reads that came true, and write the count in the report.
Never use a read made after the cutoff. Never fill a morning status from a postgame feed.

## Mixture

For a game with home candidates `h_i` and away candidates `a_j`:

`P(home win) = sum_i sum_j p_i * q_j * predict_logistic(fit, features(h_i, a_j))`

The two sides are independent: the starter choice of one team does not depend on the other team's choice. Say so in the docstring.
Build each pair's features with the pair's goalies set on the `Game` (`home_goalie`, `away_goalie`), so the existing code path gives the pair's prediction.
Cap the pairs at `MAX_PAIRS = 9` by keeping the three most likely goalies of each side and renormalizing. Name the dropped mass in a note when it is above 1%.

`NhlGameModel(..., starter_mixture: bool = False)` turns it on. The default stays off until a record promotes it.
`GamePrediction` gets a new field `goalie_scenarios: tuple[GoalieScenario, ...] = ()`, where `GoalieScenario` holds the home goalie, the away goalie, the weight, and the pair's `home_win`.
The goal intervals stay the single-guess intervals until #52 supports draws. With draws, each draw first picks a pair by its weight.

## Scores

1. Starter model: multiclass log loss and top-1 accuracy of `starter_candidates` against the boxscore starter, by season, against today's single guess (top-1 accuracy only, because a probability of 1 has an infinite log loss when wrong).
2. Game model: with a fit that holds a goalie feature, the paired winner log loss of the mixture against the single guess, in the guess run.

## Tests

- Weights are at least 0 and sum to 1.
- A confirmed starter gives one candidate, and the mixture prediction equals today's prediction exactly.
- A zero-weight pair does not change the result.
- An injured or departed goalie is never a candidate.
- No history gives no candidate and the existing note.
- A worked example with a goalie feature weight of 2 shows that the mixture of final probabilities differs from the prediction at the averaged goalie value.
- Future-invariance (H5) for `starter_candidates`.

## Calls made

- A three-parameter softmax, because it adds the back-to-back pattern and the likely reads to the current count rule, and three numbers fit on a small sample.
- Independent sides, because one team's starter choice does not depend on the other team's.
- Off by default, because the live fit has no goalie feature.
