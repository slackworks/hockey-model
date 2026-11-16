"""The types of the read-only consumer API (#80). The errors are in ``consumer_errors``.

Every type here is frozen, so a caller can keep one and compare it later. Every time is a
UTC-aware ``datetime``. The module holds no logic that reads the store.
"""

from dataclasses import dataclass
from datetime import date, datetime

from hockey_model.game import GamePrediction, GoalieStart, MissingInput
from hockey_model.sources.nhl import FINAL_STATES
from hockey_model.utc import require_utc

# The market that a prediction answers: one winner after overtime and the shootout.
FULL_GAME_WINNER = "full_game_winner"
# The inputs of every result are today's rows, not the rows that existed at `as_of`. #36 adds
# the archived and strict modes.
RESTATEMENT = "restatement"


@dataclass(frozen=True, slots=True)
class GameInfo:
    """One stored game, as the newest read of the feed describes it."""

    game_id: int
    season: int
    game_type: int
    start: datetime
    home: str
    away: str
    state: str
    final: bool
    home_score: int | None
    away_score: int | None
    last_period: str | None
    fetched_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.start, "start")
        require_utc(self.fetched_at, "fetched_at")
        if self.final != (self.state in FINAL_STATES):
            raise ValueError(
                f"game {self.game_id}: final is {self.final} but state is {self.state!r}; "
                "a final game has a final state"
            )


@dataclass(frozen=True, slots=True)
class StarterInfo:
    """The goalie expected to start for one team, and how sure the store is.

    ``confirmed`` is true only when a source confirmed the start. A guess from the earlier
    starts is not confirmed, and ``missing`` says why.
    """

    game_id: int
    team: str
    goalie: GoalieStart | None
    confirmed: bool
    missing: tuple[MissingInput, ...]


@dataclass(frozen=True, slots=True)
class SkaterLine:
    """One skater's line from a final boxscore."""

    game_id: int
    team: str
    nhl_id: int
    toi_seconds: int | None
    goals: int | None
    assists: int | None


@dataclass(frozen=True, slots=True)
class GoalieLine:
    """One goalie's line from a final boxscore."""

    game_id: int
    team: str
    nhl_id: int
    toi_seconds: int | None
    started: bool


@dataclass(frozen=True, slots=True)
class TeamXgLine:
    """One MoneyPuck team game: the expected goals for and against a team."""

    game_id: int
    team: str
    game_date: date
    xg_for: float
    xg_against: float


@dataclass(frozen=True, slots=True)
class GoalieXgLine:
    """One MoneyPuck goalie game: the expected goals against and the goals against."""

    game_id: int
    nhl_id: int
    xg_against: float
    goals_against: int


@dataclass(frozen=True, slots=True)
class InjuryLine:
    """One player on the injury list of one read of the list."""

    team: str
    player_name: str
    status: str
    return_date: date | None
    read_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.read_at, "read_at")


@dataclass(frozen=True, slots=True)
class FitInfo:
    """The saved fit that a consumer serves, with the values that name it."""

    fit_id: int
    labels: tuple[str, ...]
    fit_through_season: int | None
    features: tuple[str, ...]
    fit_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class StoreInfo:
    """The store that a consumer reads: its path, its schema, and the newest read of each source."""

    path: str
    schema_version: int
    newest_reads: tuple[tuple[str, datetime | None], ...]


@dataclass(frozen=True, slots=True)
class ConsumerPrediction:
    """One game's prediction, with the game, the fit, the store, and how it was made.

    ``prediction.home_goals`` and ``prediction.away_goals`` are marginal intervals for one
    team's goals. They are not a joint score distribution: two marginals do not give the
    chance of a total, or the chance that either team wins by a margin.
    """

    prediction: GamePrediction
    game: GameInfo
    fit: FitInfo
    store: StoreInfo
    as_of: datetime
    outcome: str
    reconstruction: str
    package_version: str

    def __post_init__(self) -> None:
        require_utc(self.as_of, "as_of")

    @property
    def made_after_start(self) -> bool:
        """Return whether the prediction time is at or after the puck drop."""
        return self.prediction.made_after_start
