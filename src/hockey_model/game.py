"""Public input and output types for the NHL game win probability model."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from hockey_model.matchup import check_win_pair
from hockey_model.models import GameModel
from hockey_model.team_codes import NHL_TEAM_CODES
from hockey_model.types import HockeyModelError, Player, Projection, ProjectionError
from hockey_model.utc import require_utc
from hockey_model.validation import Domain, validate_projection


class StarterStatus(StrEnum):
    """How certain a goalie start is."""

    CONFIRMED = "confirmed"
    LIKELY = "likely"
    UNKNOWN = "unknown"


class StarterSource(StrEnum):
    """Which source reported a goalie start."""

    ESPN = "espn"
    DAILY_FACEOFF = "daily_faceoff"
    NHL_BOXSCORE = "nhl_boxscore"
    MODEL_GUESS = "model_guess"


class MissingKind(StrEnum):
    """A model input that was absent or weak, so the prediction is less certain."""

    GOALIE_UNCONFIRMED = "goalie_unconfirmed"
    GOALIE_UNKNOWN = "goalie_unknown"
    XG_FORM = "xg_form"
    ATTEMPT_FORM = "attempt_form"
    GOALIE_FORM = "goalie_form"
    STALE_SOURCE = "stale_source"
    LATE_RUN = "late_run"
    REST_UNKNOWN = "rest_unknown"


@dataclass(frozen=True, slots=True)
class GoalieStart:
    """A goalie expected to start for one team, with its source and read time."""

    player: Player
    status: StarterStatus
    source: StarterSource
    read_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.read_at, "read_at")


def _require_team_code(role: str, code: str) -> None:
    if code not in NHL_TEAM_CODES:
        known = " ".join(sorted(NHL_TEAM_CODES))
        raise HockeyModelError(
            f"{role} is an unknown NHL team code {code!r}; use one of the known codes: {known}"
        )


@dataclass(frozen=True, slots=True)
class Game:
    """One scheduled NHL game, with optional goalie starts."""

    game_id: int
    home: str
    away: str
    start: datetime
    home_goalie: GoalieStart | None = None
    away_goalie: GoalieStart | None = None

    def __post_init__(self) -> None:
        if self.game_id <= 0:
            raise HockeyModelError(f"game_id must be positive, got {self.game_id}")
        home = self.home.strip()
        away = self.away.strip()
        if not home or not away:
            raise HockeyModelError("home and away team codes must not be empty")
        if home == away:
            raise HockeyModelError(f"home and away must differ, both are {home!r}")
        _require_team_code("home", self.home)
        _require_team_code("away", self.away)
        require_utc(self.start, "start")


@dataclass(frozen=True, slots=True)
class MissingInput:
    """One input the model did not have, or had only in a weak form."""

    kind: MissingKind
    team: str | None
    note: str


@dataclass(frozen=True, slots=True)
class DegradedInput:
    """A model feature that was set to its neutral value, because a team had no data for it.

    ``teams`` names the teams without data, home first. A feature that was measured, even when
    it measured zero, is never listed.
    """

    feature: str
    kind: MissingKind
    teams: tuple[str, ...]


class TrainingMembership(StrEnum):
    """Whether the seasons that trained the fit include the predicted game."""

    IN_SAMPLE = "in_sample"
    OUT_OF_SAMPLE = "out_of_sample"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TrainingInfo:
    """The fit that made a prediction, and whether the game trained it.

    ``membership`` is ``unknown`` when the saved fit does not record the season that it
    trained through. Such a fit may still include the game, so a reader must not read
    ``unknown`` as out of sample.
    """

    fit_id: int
    fit_through_season: int | None
    membership: TrainingMembership


@dataclass(frozen=True, slots=True)
class GamePrediction:
    """Win probabilities and goal projections for one game."""

    home_win: float
    away_win: float
    home_goals: Projection
    away_goals: Projection
    missing: tuple[MissingInput, ...]
    credits: tuple[str, ...]
    made_after_start: bool = False
    degraded_inputs: tuple[DegradedInput, ...] = ()
    # None only for a prediction that a model made without a saved fit, such as a test model.
    training: TrainingInfo | None = None


def game_win_probability(
    game: Game,
    model: GameModel,
    *,
    as_of: datetime | None = None,
    level: float = 0.8,
) -> GamePrediction:
    """Predict a game as known at ``as_of``, defaulting to the current UTC time."""
    if not 0.0 < level < 1.0:
        raise HockeyModelError(f"level must be inside the open interval (0, 1), got {level}")
    moment = datetime.now(UTC) if as_of is None else as_of
    require_utc(moment, "as_of")
    result = model.predict(game, moment, level)
    label = f"predict game {game.game_id} ({game.away} at {game.home})"
    if not isinstance(result, GamePrediction):
        raise ProjectionError(
            f"{label}: the model returned {type(result).__name__}, expected a GamePrediction; "
            "return a GamePrediction"
        )
    check_win_pair((result.home_win, result.away_win), label)
    validate_projection(result.home_goals, level, f"{label}: home goals", Domain.NONNEGATIVE)
    validate_projection(result.away_goals, level, f"{label}: away goals", Domain.NONNEGATIVE)
    made_after_start = moment >= game.start
    missing = result.missing
    if made_after_start:
        late = MissingInput(
            MissingKind.LATE_RUN,
            None,
            "prediction made after puck drop; inputs are as of the game start",
        )
        missing = (*result.missing, late)
    return replace(result, missing=missing, made_after_start=made_after_start)
