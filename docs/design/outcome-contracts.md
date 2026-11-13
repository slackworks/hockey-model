# Outcome distribution contract (#83)

## Goal

Define how a consumer asks for a sporting outcome other than the full-game winner, and how hockey-model says "not supported".
This issue writes the contract, the types, and the tests on synthetic draws.
It ships no new model. Each capability goes live only when its model issue passes its own test.

## Capability matrix

| Capability | Name | Status now | Model issue |
| --- | --- | --- | --- |
| Full-game winner, OT and shootout included | `full_game_winner` | supported (analytic) | shipped |
| Regulation result: home, draw, away | `regulation_result` | unsupported | #52 |
| Score distribution: totals and margins | `game_score` | unsupported | #52 |
| Player counts in one game | `player_game_counts` | unsupported | #53, #55 |
| Roster totals over a horizon | `roster_totals` | unsupported | #52, #53 |
| Season standings and playoff qualification | `season` | unsupported | #60 |
| Playoff rounds and the Cup | `playoff_bracket` | unsupported | #61 |
| In-play, from a game state | `in_play` | unsupported, no issue | none |

`hockey_model.capabilities() -> tuple[Capability, ...]` returns the matrix with a `supported` flag.
A call for an unsupported capability raises `UnsupportedCapabilityError` (from [consumer-api.md](consumer-api.md)) that names the capability.
A missing input is never this error: it is a `MissingInput` in the result.
A call after puck drop for a pregame capability returns the pregame answer with `made_after_start` set, as the winner does. It is never an in-play answer.

## Representation: shared draws

A mean and an interval cannot give `P(total >= 6)` or a joint event.
So every capability except the analytic winner returns draws from one simulation, and every event is a function of the same draws.

```python
@dataclass(frozen=True, slots=True)
class GameDraws:
    game_id: int
    as_of: datetime
    fit_id: int
    seed: int
    home_regulation: numpy.ndarray  # int, shape (n,): goals in the first 60 minutes
    away_regulation: numpy.ndarray
    decided_in: numpy.ndarray  # str codes, shape (n,): "REG", "OT", "SO"
    home_wins: numpy.ndarray  # bool, shape (n,): the final winner
    missing: tuple[MissingInput, ...]

    @property
    def home_on_ice(
        self,
    ) -> numpy.ndarray: ...  # regulation goals plus an OT goal; no shootout goal
    @property
    def home_final_score(
        self,
    ) -> numpy.ndarray: ...  # the official score: plus 1 for the shootout winner
```

Rules:

1. One draw is one whole game. A player or a roster capability adds arrays of the same length `n` to the same draw, so a player's goals and the team's goals agree in every draw.
2. `seed` and `n` are arguments. The same seed, store snapshot, and fit give the same draws.
3. Every probability comes back as `Estimate(value, mc_error)`, where `mc_error = sqrt(p * (1 - p) / n)`.
4. An event is a function `GameDraws -> numpy.ndarray[bool]`. The module `hockey_model.events` gives the common ones: `total_at_least(k, score="on_ice")`, `margin_at_least(team, k, score="final")`, `regulation_draw()`, `final_winner(team)`, and `both(e1, e2)`. A threshold is an integer and the comparison is `>=`. A consumer that needs "over 5.5" passes `k = 6`.
5. `probability(draws, event) -> Estimate` is the mean of the event array.
6. The draws' final winner MUST agree with the analytic winner model within Monte Carlo error. #52 does this by fitting the goal rates so that the draws' home win share equals `home_win` of the winner model (an "anchor"). The contract test checks the agreement.
7. Never multiply two marginal probabilities to get a joint one. The only joint path is an event on shared draws.

## Score conventions

- `on_ice` counts goals scored in play: regulation and overtime. A shootout adds no goal.
- `final` is the NHL official score: the shootout winner gets one goal.
- A regular season overtime is one sudden-death period, then a shootout. A playoff overtime repeats until a goal. `decided_in` is never `"SO"` for a playoff game.
- A tied regulation score is a regulation draw. The two-way winner pair alone MUST NOT be used to derive it.

Market settlement rules stay in sportsbettr.

## Code in this issue

1. `src/hockey_model/outcomes.py`: `Capability`, `capabilities()`, `GameDraws`, `Estimate`, `probability`.
2. `src/hockey_model/events.py`: the event constructors of rule 4.
3. `ConsumerStore.game_draws(game_id, *, as_of, n, seed)` raises `UnsupportedCapabilityError("game_score")` until #52 sets the capability to supported.
4. README section "Outcomes" with the matrix and the conventions.

## Tests

On synthetic `GameDraws` made by hand:

- Every `Estimate` is in [0, 1].
- `P(home wins) + P(away wins) = 1`, and the three regulation results sum to 1.
- `total_at_least(k)` falls as `k` grows.
- `P(both(a, b)) <= min(P(a), P(b))`.
- `home_final_score` minus `home_on_ice` is 1 exactly in the shootout draws that home won, and 0 in every other draw.
- The same seed gives the same arrays.
- Every unsupported capability raises `UnsupportedCapabilityError` with its name.

## Follow-ups

Each supported capability needs its model issue: #52 for `regulation_result` and `game_score`, #53 and #55 for players, #60 and #61 for the season.
This design is the target shape for those issues. They do not need to succeed for this issue to close.

## Calls made

- Draws, not a closed list of event types, because draws answer any joint event and keep the dependence between teams and players.
- Integer thresholds with `>=` only, so there is one comparison rule.
- The anchor rule, so the winner from the draws and the winner from the analytic model never disagree by more than Monte Carlo error.
