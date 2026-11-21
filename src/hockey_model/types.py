"""Typed values and domain errors for the public hockey-model API."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class HockeyModelError(Exception):
    """Base class for caller-actionable hockey-model errors."""


class AmbiguousPlayerError(HockeyModelError):
    """Raised when a player reference matches more than one player."""

    def __init__(self, ref: str, candidates: tuple[Player, ...]) -> None:
        self.ref = ref
        self.candidates = candidates
        choices = ", ".join(f"{player.canonical_id} ({player.full_name})" for player in candidates)
        super().__init__(
            f"resolve player {ref!r}: multiple players match ({choices}); retry with an ID"
        )


class UnknownPlayerError(HockeyModelError):
    """Raised when a player reference has no match."""

    def __init__(self, ref: str | int) -> None:
        self.ref = ref
        super().__init__(f"resolve player {ref!r}: no player matches; check the name or use an ID")


class SlotError(HockeyModelError):
    """Raised when a roster slot assignment is invalid."""


class MissingProjectionError(HockeyModelError):
    """Raised when a required model projection is unavailable."""


class RateStatAggregationError(HockeyModelError):
    """Raised when a caller asks to sum a rate statistic."""


class ProbabilityError(HockeyModelError):
    """Raised when model output is not a valid probability distribution."""


class ProjectionError(HockeyModelError):
    """Raised when a projection, or an input to one, is malformed or out of range."""


class ModelNotConfiguredError(HockeyModelError):
    """Raised when the CLI has no runtime model configuration."""


class Slot(StrEnum):
    """A supported roster position or bench assignment."""

    C = "C"
    W = "W"
    D = "D"
    G = "G"
    UTIL = "UTIL"
    BENCH = "bench"


def _require_text(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be empty")


@dataclass(frozen=True, slots=True)
class Player:
    """A player with stable source identities and position eligibility."""

    canonical_id: str
    yahoo_id: str | None
    nhl_id: int | None
    full_name: str
    eligible: frozenset[Slot] | None
    team_code: str | None

    def __post_init__(self) -> None:
        _require_text(self.canonical_id, "canonical_id")
        _require_text(self.full_name, "full_name")
        if self.eligible is None:
            raise ValueError("eligible must be a frozenset of slot strings")
        if not self.eligible:
            raise ValueError("eligible must contain at least one position")
        invalid = self.eligible - {Slot.C, Slot.W, Slot.D, Slot.G}
        if invalid:
            raise ValueError(f"eligible contains roster-only slots: {sorted(invalid)}")
        if self.yahoo_id is not None:
            _require_text(self.yahoo_id, "yahoo_id")
        if self.nhl_id is not None and self.nhl_id <= 0:
            raise ValueError("nhl_id must be positive")
        if self.team_code is not None:
            _require_text(self.team_code, "team_code")


@dataclass(frozen=True, slots=True)
class Team:
    """An NHL team."""

    code: str
    name: str
    division: str
    conference: str

    def __post_init__(self) -> None:
        for field in ("code", "name", "division", "conference"):
            _require_text(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class Standing:
    """A team's current regular-season standing."""

    team_code: str
    games_played: int
    points: int
    regulation_overtime_wins: int
    rank: int

    def __post_init__(self) -> None:
        _require_text(self.team_code, "team_code")
        for field in ("games_played", "points", "regulation_overtime_wins"):
            if getattr(self, field) < 0:
                raise ValueError(f"{field} must not be negative")
        if self.rank <= 0:
            raise ValueError("rank must be positive")


@dataclass(frozen=True, slots=True)
class Roster:
    """A named collection of players with optional slot assignments."""

    name: str
    players: tuple[Player, ...]
    slots: Mapping[str, Slot] | None = None

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        ids = [player.canonical_id for player in self.players]
        if len(ids) != len(set(ids)):
            raise ValueError("players must have unique canonical_id values")


@dataclass(frozen=True, slots=True)
class Projection:
    """A point estimate and uncertainty interval at a stated level."""

    point: float
    low: float
    high: float
    level: float

    def __post_init__(self) -> None:
        for field in ("point", "low", "high", "level"):
            if not math.isfinite(getattr(self, field)):
                raise ValueError(f"{field} must be finite")
        if not 0.0 < self.level < 1.0:
            raise ValueError("level must be inside the open interval (0, 1)")
        if self.low > self.high:
            raise ValueError("low must not be above high")
