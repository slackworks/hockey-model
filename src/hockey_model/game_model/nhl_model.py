"""The NHL game win model: Elo plus fitted features. It reads the store and fetches nothing."""

import math
import sqlite3
from datetime import datetime

from hockey_model.game import (
    Game,
    GamePrediction,
    MissingInput,
    MissingKind,
    TrainingInfo,
    TrainingMembership,
)
from hockey_model.game_day import game_day_of
from hockey_model.game_model.elo import replay
from hockey_model.game_model.features import FORM_WINDOW, GoalieMode, build_features
from hockey_model.game_model.logistic import predict_logistic
from hockey_model.game_model.seasons import ELO_BURN_IN, season_of
from hockey_model.sources.moneypuck import MONEYPUCK_CREDIT
from hockey_model.sources.teams import franchise
from hockey_model.store.fits import FitArtifact
from hockey_model.store.moneypuck_gaps import mp_form_lag
from hockey_model.store.reads import (
    final_games_before,
    game_row,
    mp_team_form_before,
    team_games_before,
)
from hockey_model.store.staleness import stale_sources
from hockey_model.types import Projection, ProjectionError

# Goals per team and game when a team has no games in the store. The NHL average is near 3.
LEAGUE_GOALS = 3.0
# A rate below this is raised to it, and the prediction says so. A rate of zero has no spread.
_MIN_RATE = 0.1
# The largest expected goals for one team that the goal interval accepts. The walk starts at
# exp(-rate), which is a normal float for any rate below about 700. A hockey team scores about
# 3 goals, so the bound is far above any real rate, and well inside the stable range.
MAX_RATE = 100.0
# The most steps that the walk to a quantile may take. At MAX_RATE the 1e-12 tail needs fewer
# than half of them, so a walk that uses all of them is a bug, not a wide interval.
MAX_QUANTILE_STEPS = 1000


def _poisson_quantile(rate: float, probability: float) -> int:
    """Return the smallest count whose cumulative Poisson chance reaches ``probability``.

    ``rate`` is in [0, MAX_RATE] and ``probability`` is in (0, 1), and the caller checks both.
    The walk raises when it ends before it reaches ``probability``, because the loop guard is
    not a quantile.
    """
    term = math.exp(-rate)
    total = term
    count = 0
    while total < probability:
        if count == MAX_QUANTILE_STEPS:
            raise ProjectionError(
                f"goal interval: no count up to {MAX_QUANTILE_STEPS} reaches the chance "
                f"{probability!r} at {rate!r} expected goals; use a lower level or a lower rate"
            )
        count += 1
        term *= rate / count
        total += term
    return count


def _check_projection_inputs(rate: float, level: float) -> None:
    """Raise ``ProjectionError`` unless the rate and the level are in the supported domain."""
    if not math.isfinite(rate) or not 0.0 <= rate <= MAX_RATE:
        raise ProjectionError(
            f"goal interval: the expected goals {rate!r} are not a finite number from 0 to "
            f"{MAX_RATE:g}; check the goal rates of the team"
        )
    if not math.isfinite(level) or not 0.0 < level < 1.0:
        raise ProjectionError(
            f"goal interval: the level {level!r} is not inside the open interval (0, 1); "
            "pass a level such as 0.8"
        )
    if (1.0 + level) / 2.0 >= 1.0:
        raise ProjectionError(
            f"goal interval: the level {level!r} is so close to 1 that its upper tail rounds to "
            "1.0 in floating point; pass a smaller level"
        )


def _projection(rate: float, level: float, where: str) -> Projection:
    """Return the goal interval of one side. ``where`` names the game and the side in an error."""
    try:
        _check_projection_inputs(rate, level)
        rate = max(rate, _MIN_RATE)
        low = _poisson_quantile(rate, (1.0 - level) / 2.0)
        high = _poisson_quantile(rate, (1.0 + level) / 2.0)
    except ProjectionError as exc:
        raise ProjectionError(f"{where}: {exc}") from exc
    return Projection(rate, min(float(low), rate), max(float(high), rate), level)


def _goals_for_and_against(
    conn: sqlite3.Connection, team: str, cutoff: datetime
) -> tuple[list[float], list[float], bool]:
    """Return the goals a team's games made and allowed, and whether xG was used."""
    rows = mp_team_form_before(conn, team, cutoff, FORM_WINDOW)
    if rows:
        return [made for made, _ in rows], [allowed for _, allowed in rows], True
    scored: list[float] = []
    allowed: list[float] = []
    for game in team_games_before(conn, team, cutoff, FORM_WINDOW):
        mine_home = franchise(game.home) == franchise(team)
        if game.home_score is None or game.away_score is None:
            raise ValueError(f"read game {game.game_id}: a final game has no score; refresh again")
        home_score, away_score = game.home_score, game.away_score
        scored.append(home_score if mine_home else away_score)
        allowed.append(away_score if mine_home else home_score)
    return scored, allowed, False


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _training_info(artifact: FitArtifact, season: int, start: datetime) -> TrainingInfo:
    """Return the fit that made a prediction, and whether the game trained it.

    The check needs both bounds: the saved season, and the start of the newest game that the
    fit read. The season alone names every later game of that season in sample, which is the
    live-fit case (#238). A fit that does not record its season gives ``unknown``.
    """
    cutoff = artifact.fit_through_season
    covered = artifact.covers_game(season, start)
    if covered is None:
        membership = TrainingMembership.UNKNOWN
    elif covered:
        membership = TrainingMembership.IN_SAMPLE
    else:
        membership = TrainingMembership.OUT_OF_SAMPLE
    return TrainingInfo(artifact.fit_id, cutoff, membership)


def _stored_game(conn: sqlite3.Connection, game: Game) -> tuple[int, datetime]:
    """Return the stored season and start of ``game``, or those of its Eastern game day.

    The stored season is the name the fit trained on. The 2020 bubble is the case that
    differs: its games are stored under 20192020, and their September starts fall in the
    Eastern game day of 2020-21.
    """
    row = game_row(conn, game.game_id)
    if row is None:
        return season_of(game_day_of(game.start)), game.start
    return row.season, row.start


def _clamp_notes(game: Game, home_rate: float, away_rate: float) -> list[MissingInput]:
    """Name each side whose expected goals are raised to the floor."""
    return [
        MissingInput(MissingKind.XG_FORM, team, f"expected goals under {_MIN_RATE} are raised")
        for team, rate in ((game.home, home_rate), (game.away, away_rate))
        if rate < _MIN_RATE
    ]


class NhlGameModel:
    """Predict a game from the store, as known at the prediction time."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        artifact: FitArtifact,
        goalie_mode: GoalieMode = GoalieMode.READS,
    ) -> None:
        self._conn = conn
        self._artifact = artifact
        self._fit = artifact.fit
        self._mode = goalie_mode

    def _expected_goals(
        self, game: Game, cutoff: datetime
    ) -> tuple[float, float, list[MissingInput], bool]:
        """Return both goal rates, their gaps, and whether MoneyPuck data set a rate."""
        home_for, home_against, home_xg = _goals_for_and_against(self._conn, game.home, cutoff)
        away_for, away_against, away_xg = _goals_for_and_against(self._conn, game.away, cutoff)
        notes: list[MissingInput] = []
        rates: list[float] = []
        for team, own, theirs, used_xg in (
            (game.home, home_for, away_against, home_xg),
            (game.away, away_for, home_against, away_xg),
        ):
            attack, defence = _mean(own), _mean(theirs)
            if attack is None or defence is None:
                note = f"no games before this game; {LEAGUE_GOALS} goals is used"
                notes.append(MissingInput(MissingKind.XG_FORM, team, note))
                rates.append(LEAGUE_GOALS)
                continue
            if not used_xg:
                note = "the expected goals come from actual goals, not MoneyPuck expected goals"
                notes.append(MissingInput(MissingKind.XG_FORM, team, note))
            else:
                lag = mp_form_lag(self._conn, team, cutoff)
                if lag is not None:
                    notes.append(MissingInput(MissingKind.STALE_SOURCE, team, lag.note()))
            rates.append((attack + defence) / 2)
        return rates[0], rates[1], notes, home_xg or away_xg

    def predict(self, game: Game, as_of: datetime, level: float) -> GamePrediction:
        """Return the win chances and expected goals of ``game`` as known at ``as_of``."""
        cutoff = min(as_of, game.start)
        elo = replay(g for g in final_games_before(self._conn, cutoff) if g.season >= ELO_BURN_IN)
        row = build_features(
            self._conn, game, as_of, elo, goalie_mode=self._mode, names=self._fit.features
        )
        home_win = predict_logistic(self._fit, row)
        home_rate, away_rate, goal_notes, goals_use_moneypuck = self._expected_goals(game, cutoff)
        missing = [*row.missing, *goal_notes]
        missing.extend(
            MissingInput(MissingKind.STALE_SOURCE, None, f"{source}: {reason}")
            for source, reason in stale_sources(self._conn, cutoff)
        )
        missing.extend(_clamp_notes(game, home_rate, away_rate))
        # The goal rates read MoneyPuck data even when the fit uses no MoneyPuck feature.
        credits = (MONEYPUCK_CREDIT,) if row.uses_moneypuck or goals_use_moneypuck else ()
        return GamePrediction(
            home_win=home_win,
            away_win=1.0 - home_win,
            home_goals=_projection(home_rate, level, f"game {game.game_id} {game.home} (home)"),
            away_goals=_projection(away_rate, level, f"game {game.game_id} {game.away} (away)"),
            missing=tuple(missing),
            credits=credits,
            degraded_inputs=row.imputed,
            training=_training_info(self._artifact, *_stored_game(self._conn, game)),
        )
