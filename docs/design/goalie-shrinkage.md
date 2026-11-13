# Goalie GSAx shrunk by exposure (#45)

Uses H1 to H5, H7, H8 of [research-harness.md](research-harness.md).

## Goal

Replace the `GOALIE_WINDOW`-game mean of goals saved above expected (`goalie_gsax`) with an empirical-Bayes estimate that trusts a goalie in proportion to the shots that the goalie faced.
Give each estimate a variance, so #49 and #52 can carry the uncertainty.

## Data readiness

- Ready now: `mp_goalie_games` holds xGA (`xg_against`) and goals against for each goalie game, all situations.
- The exposure is the xGA faced. Vince's test in the issue found minutes a poor proxy, and xGA is in the store.
- Optional second exposure: unblocked shots and shots on goal from [moneypuck-situations.md](moneypuck-situations.md). Report it as a variant only when that table exists.

## Model

For goalie `g` before the cutoff, with the games `k` of that goalie's history:

- `S = sum_k w_k * (xga_k - ga_k)`: weighted goals saved above expected.
- `X = sum_k w_k * xga_k`: weighted exposure.
- `w_k = 0.5 ** (days_before_cutoff_k / HALF_LIFE_DAYS)`. `HALF_LIFE_DAYS = None` means no decay (`w_k = 1`).

Talent `t` is goals saved above expected per expected goal against.
Under the null of no talent, `xga - ga` has variance close to `xga` (Poisson goals), so `S | t ~ Normal(t * X, X)`.
The prior is `t ~ Normal(m, 1 / K)`, where `m` is the prior mean and `K` the prior strength in units of xGA.
The posterior is:

- mean `t_hat = (S + K * m) / (X + K)`
- variance `1 / (X + K)`

Two prior means: `m_new` for a goalie with no NHL game before the season, and `m_known` for every other goalie.
A call-up is therefore pulled toward a replacement level, and an established goalie toward the league.

Fit `m_new`, `m_known`, and `K` by maximum likelihood on goalie-seasons of `SELECT_TRAIN`.
For one goalie-season, `S ~ Normal(m * X, X + X**2 / K)`.
Minimize the negative log-likelihood with `scipy.optimize.minimize` over `(m_new, m_known, log K)`.
Choose `HALF_LIFE_DAYS` from `{None, 365, 730}` by the predictive log-likelihood of the next season's `S` on `SELECT_TRAIN`, with the other three parameters fit for each choice.
The script is `scripts/research/fit_goalie_prior.py`. Its printed values become module constants (H2 rule 7).

## Feature

New module `game_model/goalie_rates.py`:

```python
@dataclass(frozen=True, slots=True)
class GoaliePosterior:
    mean: float
    variance: float
    exposure: float       # X
    games: int
    prior: str            # "new" or "known"

def goalie_posterior(conn, nhl_id: int, cutoff: datetime) -> GoaliePosterior: ...
```

Register feature `goalie_gsax_eb` (H2): home starter `t_hat` minus away starter `t_hat`.
It uses the same starter resolution as `goalie_gsax` (`context.starters`).
A starter with no history gets the prior mean. That is a model estimate, not an imputation, so it is not a `DegradedInput`. It adds a `MissingInput` of kind `GOALIE_FORM` with the note "no games before this game; the prior is used", so the report counts it.
No starter, or a starter with no NHL id, gives `NEUTRAL_FORM` and a `DegradedInput`, as `goalie_gsax` does.

## Variants in the report

| Variant | Value |
| --- | --- |
| V0 | `goalie_gsax`: the `GOALIE_WINDOW`-game mean |
| V1 | prior mean only |
| V2 | `goalie_gsax_eb`, no decay |
| V3 | `goalie_gsax_eb` with the chosen half-life |

For each: the paired winner log loss against the reference `xg_form, rest` (H3), the actual and the guess run, and the standard deviation of the home-minus-away feature over the scored games.
Vince's test found a spread of 0.26 GSAx per 60 after shrinkage. Report the spread so a reader can see why a weight moves the forecast little.

Also report, on the exploration folds: the log score of next-season `S` given the posterior (`Normal(t_hat * X_next, X_next + X_next**2 * variance)`), and 80% interval coverage of next-season `S`.

## Tests

- No history: the posterior is the prior.
- Equal `S / X`, larger `X`: the posterior mean is nearer `S / X`, and the variance is smaller.
- A relief appearance with tiny `X` barely moves the mean.
- A trade does not reset the history: the goalie id is stable.
- The likelihood fit recovers `m` and `K` within 10% on 400 simulated goalie-seasons drawn from the model.
- Future-invariance (H5): a goalie game after the cutoff does not change `goalie_posterior`.

## Experiment record

`experiments/00NN-goalie-gsax-eb.json`, cohort `exploration`, `candidate_features: ["xg_form", "rest", "goalie_gsax_eb"]`, `reference_features: ["xg_form", "rest"]`.

## Calls made

- xGA as the exposure, because it is in the store and it measures the chance quality faced. Shots are a variant when the MoneyPuck situations table exists.
- A normal approximation with the Poisson variance, because it gives a closed-form posterior and a two-line likelihood.
- Two prior means instead of one, so a call-up is not treated as an average goalie.
