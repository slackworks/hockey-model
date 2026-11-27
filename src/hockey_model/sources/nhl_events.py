"""Read the NHL play-by-play of one game. This module fetches and parses; it stores nothing.

The feed lists every event of a game with its clock, its on-ice situation, and its players.
A parsed event keeps the raw feed coordinates and the attacking direction that the feed gives.
Design: docs/design/pbp-shift-dataset.md
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from hockey_model.sources.http import HttpClient
from hockey_model.sources.payload import (
    as_list,
    as_mapping,
    field,
    get_json,
    int_field,
    str_field,
)
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.team_codes import NHL_TEAM_CODES

SOURCE = "nhl_pbp"
PBP_BASE = "https://api-web.nhle.com/v1/gamecenter"
# Change the version when a parser change can change a parsed row.
PBP_PARSER = "nhl_pbp:1"
PERIOD_TYPES = frozenset({"REG", "OT", "SO"})
PERIOD_SECONDS = 1200
# The largest id that SQLite stores as an integer, and the most periods that a game can have.
# A larger value is a feed fault, and the parser names it before the store sees it.
MAX_ID = 2**63 - 1
MAX_PERIOD = 20
# ASCII digits only, so a Unicode digit never parses as a time.
_CLOCK = re.compile(r"([0-9]{1,3}):([0-9]{2})")
_SITUATION = re.compile(r"[0-9]{4}")
# The keys of ``details`` that name the players of an event, by event type, in the order of
# player1, player2, and player3.
PLAYER_KEYS: Mapping[str, tuple[str, ...]] = {
    "goal": ("scoringPlayerId", "assist1PlayerId", "assist2PlayerId"),
    "shot-on-goal": ("shootingPlayerId",),
    "missed-shot": ("shootingPlayerId",),
    "blocked-shot": ("shootingPlayerId", "blockingPlayerId"),
    "faceoff": ("winningPlayerId", "losingPlayerId"),
    "penalty": ("committedByPlayerId", "drawnByPlayerId", "servedByPlayerId"),
    "hit": ("hittingPlayerId", "hitteePlayerId"),
    "giveaway": ("playerId",),
    "takeaway": ("playerId",),
}


@dataclass(frozen=True, slots=True)
class EventRow:
    """One play of the feed, with the facts that a later step needs."""

    event_id: int
    sort_order: int
    period: int
    period_type: str
    period_second: int
    game_second: int | None
    type: str
    situation_code: str | None
    owner_team: str | None
    x: float | None
    y: float | None
    attack_x_sign: int | None
    zone: str | None
    shot_type: str | None
    players: tuple[int | None, int | None, int | None]
    goalie_in_net: int | None
    home_score: int | None
    away_score: int | None


@dataclass(frozen=True, slots=True)
class RosterSpot:
    """One player that the feed lists for the game, with the team and the position."""

    player_id: int
    team: str
    position: str


@dataclass(frozen=True, slots=True)
class PbpRows:
    """The events and the roster of one game."""

    game_id: int
    home: str
    away: str
    roster: tuple[RosterSpot, ...]
    events: tuple[EventRow, ...]


def clock_seconds(value: object, where: str, *, limit: int = PERIOD_SECONDS) -> int:
    """Return the seconds of an ``MM:SS`` clock, or raise naming ``where``.

    A clock past ``limit`` seconds is a feed fault: a period clock past 20:00 would land inside
    the next period. ``limit`` is one period unless the caller reads a total, such as a time on
    ice.
    """
    match = _CLOCK.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match.group(2)) >= 60:
        raise SourceParseError(SOURCE, f"{where} clock must be MM:SS, got {value!r}")
    seconds = int(match.group(1)) * 60 + int(match.group(2))
    if seconds > limit:
        raise SourceParseError(SOURCE, f"{where} clock {value!r} is past the limit of {limit} s")
    return seconds


def bounded_int(source: str, obj: Mapping[str, object], key: str, where: str) -> int:
    """Return an integer field that fits the store: 0 to ``MAX_ID``."""
    value = int_field(source, obj, key, where)
    if not 0 <= value <= MAX_ID:
        raise SourceParseError(source, f"{where} field {key!r} is out of range: {value}")
    return value


def game_second(period: int, period_second: int) -> int:
    """Return the second of the game for a time in a period.

    A period of regulation or overtime starts at ``(period - 1) * 1200``, so a regular season
    overtime period starts at 3600 and a playoff overtime period ``p`` starts at
    ``3600 + (p - 4) * 1200``.
    """
    return (period - 1) * PERIOD_SECONDS + period_second


def _teams(top: Mapping[str, object], where: str) -> tuple[dict[int, str], str, str]:
    """Return the team code of each team id, the home code, and the away code."""
    found: list[tuple[int, str]] = []
    for key in ("homeTeam", "awayTeam"):
        team = as_mapping(SOURCE, field(SOURCE, top, key, where), f"{where} {key}")
        code = str_field(SOURCE, team, "abbrev", f"{where} {key}")
        if code not in NHL_TEAM_CODES:
            raise SourceShapeError(SOURCE, code)
        found.append((bounded_int(SOURCE, team, "id", f"{where} {key}"), code))
    (home_id, home), (away_id, away) = found
    if home_id == away_id or home == away:
        raise SourceParseError(SOURCE, f"{where} names one team as both teams")
    return {home_id: home, away_id: away}, home, away


def _roster(top: Mapping[str, object], codes: Mapping[int, str], where: str) -> list[RosterSpot]:
    spots: list[RosterSpot] = []
    listed = as_list(SOURCE, field(SOURCE, top, "rosterSpots", where), f"{where} rosterSpots")
    if not listed:
        raise SourceParseError(SOURCE, f"{where} lists no roster spot")
    for raw in listed:
        spot = as_mapping(SOURCE, raw, f"{where} roster spot")
        team_id = bounded_int(SOURCE, spot, "teamId", f"{where} roster spot")
        if team_id not in codes:
            raise SourceParseError(SOURCE, f"{where} roster spot names unknown team id {team_id}")
        spots.append(
            RosterSpot(
                bounded_int(SOURCE, spot, "playerId", f"{where} roster spot"),
                codes[team_id],
                str_field(SOURCE, spot, "positionCode", f"{where} roster spot"),
            )
        )
    ids = [spot.player_id for spot in spots]
    if len(set(ids)) != len(ids):
        raise SourceParseError(SOURCE, f"{where} lists a player twice in its roster")
    return spots


def _optional_int(details: Mapping[str, object], key: str, where: str) -> int | None:
    return bounded_int(SOURCE, details, key, where) if key in details else None


def _optional_number(details: Mapping[str, object], key: str, where: str) -> float | None:
    if key not in details:
        return None
    value = details[key]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise SourceParseError(SOURCE, f"{where} field {key!r} must be a number, got {value!r}")
    return float(value)


def _direction(play: Mapping[str, object], owner: str | None, home: str, where: str) -> int | None:
    """Return +1 when the owner attacks toward positive x, -1 when toward negative x.

    The feed names the side that the home team defends. The home team attacks the other side.
    No owner, or no side, gives None: the direction is never taken from the period number.
    """
    side = play.get("homeTeamDefendingSide")
    if owner is None or side is None:
        return None
    if side not in ("left", "right"):
        raise SourceParseError(SOURCE, f"{where} homeTeamDefendingSide must be left or right")
    home_attacks_positive = side == "left"
    return 1 if (owner == home) == home_attacks_positive else -1


def _players(kind: str, details: Mapping[str, object], where: str) -> tuple[int | None, ...]:
    keys = PLAYER_KEYS.get(kind, ())
    found = [_optional_int(details, key, where) for key in keys]
    found += [None] * (3 - len(found))
    return tuple(found)


def _period(play: Mapping[str, object], where: str) -> tuple[int, str]:
    descriptor = as_mapping(
        SOURCE, field(SOURCE, play, "periodDescriptor", where), f"{where} periodDescriptor"
    )
    number = bounded_int(SOURCE, descriptor, "number", f"{where} periodDescriptor")
    kind = str_field(SOURCE, descriptor, "periodType", f"{where} periodDescriptor")
    if not 1 <= number <= MAX_PERIOD or kind not in PERIOD_TYPES:
        raise SourceParseError(
            SOURCE, f"{where} period {number} of type {kind!r} is not a known period"
        )
    return number, kind


def _parse_event(raw: object, codes: Mapping[int, str], home: str) -> EventRow:
    play = as_mapping(SOURCE, raw, "a play")
    event_id = bounded_int(SOURCE, play, "eventId", "a play")
    where = f"play {event_id}"
    period, period_type = _period(play, where)
    seconds = clock_seconds(field(SOURCE, play, "timeInPeriod", where), where)
    kind = str_field(SOURCE, play, "typeDescKey", where)
    details = as_mapping(SOURCE, play.get("details") or {}, f"{where} details")
    owner_id = _optional_int(details, "eventOwnerTeamId", where)
    if owner_id is not None and owner_id not in codes:
        raise SourceParseError(SOURCE, f"{where} names unknown team id {owner_id}")
    owner = None if owner_id is None else codes[owner_id]
    situation = play.get("situationCode")
    if situation is not None and (
        not isinstance(situation, str) or _SITUATION.fullmatch(situation) is None
    ):
        raise SourceParseError(SOURCE, f"{where} situationCode must be four digits")
    one, two, three = _players(kind, details, where)
    return EventRow(
        event_id,
        bounded_int(SOURCE, play, "sortOrder", where),
        period,
        period_type,
        seconds,
        None if period_type == "SO" else game_second(period, seconds),
        kind,
        situation,
        owner,
        _optional_number(details, "xCoord", where),
        _optional_number(details, "yCoord", where),
        _direction(play, owner, home, where),
        None if "zoneCode" not in details else str_field(SOURCE, details, "zoneCode", where),
        None if "shotType" not in details else str_field(SOURCE, details, "shotType", where),
        (one, two, three),
        _optional_int(details, "goalieInNetId", where),
        _optional_int(details, "homeScore", where),
        _optional_int(details, "awayScore", where),
    )


def parse_pbp(payload: object, game_id: int) -> PbpRows:
    """Return the roster and the events of the play-by-play of game ``game_id``.

    Raises ``SourceParseError`` for a missing or mistyped field, an empty feed, a repeated event
    or player, and a payload that names another game. Raises ``SourceShapeError`` for a team code
    that the maps do not know. A renamed key and an empty list never give a silent empty game.
    """
    top = as_mapping(SOURCE, payload, "the play-by-play")
    named = bounded_int(SOURCE, top, "id", "the play-by-play")
    where = f"play-by-play {game_id}"
    if named != game_id:
        raise SourceParseError(SOURCE, f"{where} names game {named}")
    codes, home, away = _teams(top, where)
    plays = as_list(SOURCE, field(SOURCE, top, "plays", where), f"{where} plays")
    if not plays:
        raise SourceParseError(SOURCE, f"{where} lists no play; the game may not have started")
    events = [_parse_event(raw, codes, home) for raw in plays]
    seen: set[int] = set()
    for event in events:
        if event.event_id in seen:
            raise SourceParseError(SOURCE, f"{where} lists event {event.event_id} twice")
        seen.add(event.event_id)
    return PbpRows(game_id, home, away, tuple(_roster(top, codes, where)), tuple(events))


def pbp_url(game_id: int) -> str:
    """Return the URL of the play-by-play of one game."""
    return f"{PBP_BASE}/{game_id}/play-by-play"


def fetch_pbp(client: HttpClient, game_id: int) -> PbpRows:
    """Fetch and parse the play-by-play of one game."""
    return parse_pbp(get_json(client, SOURCE, pbp_url(game_id)), game_id)
