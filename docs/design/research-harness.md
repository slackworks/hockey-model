# Research harness

This design is shared by every research issue from #37 to #63.
Each issue design names the sections of this file that it uses, such as "H2".
Build H1 to H4 before the first research issue, as one pull request each.

## Problem

The research issues add features, goal models, and player models.
Four parts of the code stop that work today:

1. `game_model/features.py` holds a closed list of four features (`ALL_FEATURE_NAMES`). A new feature must edit that module, which is near its line cap.
2. `run_backtest` scores a candidate against the Elo only. A research feature must show value beyond the live model (`xg_form`, `rest`), not beyond the Elo.
3. The registry and the backtest score the home win only. Goal and player experiments have no scorer.
4. The package has no runtime dependency, so each method would be written by hand.

## H1. Dependencies

Use an existing library for every standard algorithm.
Write code by hand only for hockey rules: season formats, tiebreaks, shift timelines, and feed parsing.

| Library | Pin | Use |
| --- | --- | --- |
| `numpy` | `~=2.5.4` | Arrays for every fit and simulation. |
| `scipy` | `~=1.18.1` | `scipy.stats` distributions (`poisson`, `nbinom`, `betabinom`, `skellam`), `scipy.optimize` for marginal likelihood fits, `scipy.special.expit`/`logit`. |
| `statsmodels` | `~=0.15.0` | `GLM` with `offset` and `exposure` (Poisson, Binomial, NegativeBinomial), `fit_regularized(L1_wt=0.0)` for ridge, standard errors. It brings `pandas`. |
| `scikit-learn` | `~=1.9.1` | Only for a sparse ridge with thousands of columns (#46, #59): `Ridge`, `LogisticRegression` with `scipy.sparse` input. |

These were the current releases on PyPI on 2026-11-13, and each has a CPython 3.14 wheel.
The builder MUST check the versions again at build time (global rule), and MUST observe the 24-hour publish delay.
`ruptures` was rejected: it has no CPython 3.14 wheel.

Rules:

1. Add a library in the first pull request that needs it, not before. Add it to `[project] dependencies` in `pyproject.toml` with `~=`, then run `uv lock`.
2. Run `uv run python3 scripts/smoke_wheel.py` after the change. The wheel now installs its dependencies, so the smoke test proves the pins resolve.
3. Seed every random draw with `numpy.random.default_rng(seed)`. Never use the global NumPy or `random` state.
4. Do not rewrite `game_model/logistic.py` with a library in a research pull request. The live fit must not change. #138 owns any change to it.
5. A library result that feeds a stored value MUST be checked by a test against a small case that is solved by hand or by a second method.

## H2. Feature registry

Replace the closed feature list with a registry, so a research feature is a new module and a one-line registration.

New module `src/hockey_model/game_model/feature_registry.py`:

```python
@dataclass(frozen=True, slots=True)
class FeatureContext:
    conn: sqlite3.Connection
    game: Game
    as_of: datetime
    cutoff: datetime  # min(as_of, game.start)
    goalie_mode: GoalieMode
    catalog: Catalog
    starters: Mapping[str, GoalieStart | None]  # resolved once per game, by team
    replay: Mapping[str, object]  # replay states, by feature name


@dataclass(frozen=True, slots=True)
class FeatureValue:
    value: float  # home minus away, or NEUTRAL_FORM
    missing: tuple[MissingInput, ...]
    imputed: DegradedInput | None  # set when value is a stand-in
    used_moneypuck: bool


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    compute: Callable[[FeatureContext], FeatureValue]
    reads_moneypuck: bool
    reads_goalie: bool
    replay: Callable[[], ReplayState] | None = None  # see below


class ReplayState(Protocol):
    def update(self, game: GameRow) -> None: ...  # called after each final game, in start order
```

Rules:

1. `register(spec)` adds a spec to a module-level dict. A second spec with the same name raises `ValueError`.
2. `ALL_FEATURE_NAMES` becomes `tuple(REGISTRY)`. `MONEYPUCK_FEATURES` in `backtest/runner.py` becomes the names whose spec has `reads_moneypuck`. `GOALIE_FEATURE` checks become `reads_goalie`.
3. `FEATURES` (the production set) stays an explicit tuple in `features.py`. A registered feature is a candidate, never live, until a promoted record changes `FEATURES`.
4. Move `xg_form`, `attempt_form`, `goalie_gsax`, and `rest` into registered specs with no change in behavior. The pull request MUST show that `hockey-model backtest --games-out` gives a byte-identical JSON report and CSV on the seeded test store before and after (`tests/seeding.py`).
5. A feature that needs a sequential state, such as a rating, sets `replay`. `training_rows` and `NhlGameModel.predict` create one state per feature and call `update` after each final game, in the same loop that updates `EloBook`. The `compute` call reads the state from `context.replay[name]`, which holds only games that started before the cutoff.
6. A feature module registers itself when `hockey_model.game_model.feature_modules` imports it. That module imports every feature module, so the registry is complete before any lookup.
7. Tuning constants of a feature, such as a prior strength or a decay, are module constants. A script under `scripts/research/` chooses them on the `SELECT_TRAIN` seasons only and prints the chosen value. The pull request records the script output. A constant never comes from a scored season.

## H3. Reference comparison

A research record compares the candidate set with a reference set, so the result is the value beyond the live model.

1. Add an optional field `reference_features` (array of strings, default `null`) to `EXPERIMENT_SCHEMA` and to `IMMUTABLE_FIELDS`. `null` keeps today's behavior: the comparison is with the Elo. Record 0001 does not change.
2. `run_backtest(conn, plan, *, candidates, reference=None)`. When `reference` is set:
   - The runner fits the reference set in each walk-forward window, with no veto, and adds a run named `reference` (`REFERENCE` constant) in `RECORDED_RUNS` and in the report.
   - `ScoredRun` gets `diff_vs_reference_ci` and `mean_diff_vs_reference`, from the same block bootstrap.
   - The guess-run veto compares `reference + feature` with the reference alone, not with the Elo.
   - Success and promotion use `diff_vs_reference_ci`.
3. `hockey-model backtest --reference xg_form,rest` passes the same argument.
4. The candidate set of a record MUST contain every reference feature. The checker refuses a record that breaks this rule, with a message that names the missing feature.
5. Tests: a seeded store where the candidate equals the reference gives a difference of exactly zero. A record with `reference_features: null` gives the same report as today, byte for byte.

## H4. Count scores and research reports

Goal, player, and season experiments do not predict a home win.
They use proper scores for a count or a probability.

New module `src/hockey_model/backtest/count_metrics.py`:

- `count_log_score(pmf_values: Sequence[float]) -> list[float]`: minus the log of the predicted probability of each observed count. Floor each probability at `PROB_FLOOR`.
- `ranked_probability_score(cdf: Sequence[Sequence[float]], observed: Sequence[int]) -> list[float]`: the sum over counts of the squared difference between the predicted CDF and the step at the observed count. Truncate each CDF at `RPS_MAX_COUNT` and put the tail mass in the last cell.
- `randomized_pit(cdf_below: Sequence[float], cdf_at: Sequence[float], rng) -> list[float]`: the randomized PIT of Czado, Gneiting and Held (2009) for a discrete count. A calibrated model gives a uniform PIT.
- `interval_coverage(low, high, observed) -> float`: the share with `low <= observed <= high`. Report it with the width.

Use `scipy.stats` for every PMF and CDF. Pair per-game scores of two models and use `block_bootstrap_mean_ci` for the interval, as the winner backtest does.

The registry runs winner experiments only.
A count experiment is a research report:

1. A command `hockey-model research <topic> [--out DIR]` runs the walk-forward evaluation and writes `<topic>.json` and `<topic>.md`.
2. The JSON names the code commit, the store fingerprint (row count and newest `fetched_at` of each table), every tuning constant, and every score with its interval.
3. The Markdown report is committed under `docs/research/<date>-<topic>.md`. It states negative and inconclusive results in the same form as positive ones.
4. Folds: fit on seasons before the scored season, score that season, for each season in `EXPLORATION`. Choose tuning constants on `SELECT_TRAIN` only (H2 rule 7).
5. A count report is exploratory. It cannot promote a production change by itself. A count model that is to ship needs a registered record. Registry support for count targets is not built yet: the first issue that needs a promotion files it.

## H5. Future-invariance test kit

Every research issue tests that later data does not change an earlier value.

Add `tests/research_support.py` with one helper:

```python
def assert_future_invariant(
    build: Callable[[sqlite3.Connection], object],
    store: sqlite3.Connection,
    add_future_rows: Callable[[sqlite3.Connection], None],
) -> None:
```

It calls `build` once, adds rows that start after the cutoff with `add_future_rows`, calls `build` again, and asserts that the two results are equal.
Each feature test calls it at least once.

## H6. Data readiness

Each research issue starts with a data check.
A missing input MUST end in a written finding, never in an invented value.

| Data | Where it comes from | Design |
| --- | --- | --- |
| Team and goalie xG, goals by game | `mp_team_games`, `mp_goalie_games` (now) | none |
| Strength splits, ice time, shots on goal, rebounds, freezes, penalties, faceoffs, score-venue adjusted xG | MoneyPuck files the store already downloads | [moneypuck-situations.md](moneypuck-situations.md) |
| Events, shifts, stints, on-ice players | NHL play-by-play and shift charts | [pbp-shift-dataset.md](pbp-shift-dataset.md) (#35) |
| Raw payload revisions | every source | [payload-archive.md](payload-archive.md) (#82) |
| Birth dates | NHL player landing endpoint (needs owner approval) | [aging-adjustments.md](aging-adjustments.md) (#54) |
| Licensed or tracking data | outside sources | feasibility only (#62, #63) |

## H7. Look budget

`MAX_LOOKS` in `experiments/looks.py` caps the confirmation looks.
One research issue MUST NOT spend a confirmation look on its own.
Each issue ends with an exploration record and a research report.
#78 collects the features that pass exploration into one joint confirmation record (see [next-model-round.md](next-model-round.md)).

## H8. Done checklist for a research issue

1. The data check of H6 is written in the report, with coverage counts by season.
2. The exploration record exists in `experiments/` with `cohort: "exploration"` and `reference_features: ["xg_form", "rest"]`, or the count report of H4 exists.
3. The report lists each compared variant, its score, the paired interval against the reference, and the coverage.
4. `FEATURES` and the live fit do not change.
5. `uv run pytest`, the coverage floor script, `ruff check`, `ruff format --check`, and `ty check` pass.
6. The daily report is not changed by a research feature that is not live. AGENTS.md asks for a report change only when a new input or model becomes available to the live model.

## Calls made

- statsmodels over hand-written GLMs: it gives offsets, exposures, the negative binomial family, and standard errors in one tested place. Its cost is `pandas` in the tree.
- scikit-learn only for sparse ridge, because statsmodels works on dense arrays.
- Count experiments as reports, not registry records, until a count model needs a promotion.
- Tuning constants are chosen once on `SELECT_TRAIN` and frozen, instead of a fit inside each walk-forward window. It is simpler, and it never touches a scored season.
