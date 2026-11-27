"""Read the NHL shift chart of one game. This module fetches and parses; it stores nothing.

A row with ``typeCode`` 517 is one shift of one player. A row with ``typeCode`` 505 is a goal
event, which the play-by-play already holds, so it is not a shift. The ``duration`` of a row can
be null, so a length always comes from the start and the end.
Design: docs/design/pbp-shift-dataset.md
"""

from dataclasses import dataclass

from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl_events import (
    MAX_PERIOD,
    bounded_int,
    clock_seconds,
    game_second,
)
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

SOURCE = "nhl_shifts"
SHIFTS_URL = "https://api.nhle.com/stats/rest/en/shiftcharts"
# Change the version when a parser change can change a parsed row.
SHIFTS_PARSER = "nhl_shifts:1"
SHIFT_TYPE_CODE = 517


@dataclass(frozen=True, slots=True)
class ShiftRow:
    """One shift of one player, in seconds of the game."""

    player_id: int
    team: str
    period: int
    start_second: int
    end_second: int
    shift_number: int | None


@dataclass(frozen=True, slots=True)
class ShiftRows:
    """The shifts of one game, and the number of rows that the feed gave in all."""

    game_id: int
    shifts: tuple[ShiftRow, ...]
    rows: int


def _parse_shift(raw: object, game_id: int) -> ShiftRow | None:
    row = as_mapping(SOURCE, raw, "a shift chart row")
    where = f"shift chart row {row.get('id')!r} of game {game_id}"
    if int_field(SOURCE, row, "typeCode", where) != SHIFT_TYPE_CODE:
        return None
    if int_field(SOURCE, row, "gameId", where) != game_id:
        raise SourceParseError(SOURCE, f"{where} names another game")
    team = str_field(SOURCE, row, "teamAbbrev", where)
    if team not in NHL_TEAM_CODES:
        raise SourceShapeError(SOURCE, team)
    period = int_field(SOURCE, row, "period", where)
    if not 1 <= period <= MAX_PERIOD:
        raise SourceParseError(SOURCE, f"{where} period must be 1 to {MAX_PERIOD}, got {period}")
    start = clock_seconds(field(SOURCE, row, "startTime", where), where)
    end = clock_seconds(field(SOURCE, row, "endTime", where), where)
    number = row.get("shiftNumber")
    if number is not None:
        number = bounded_int(SOURCE, row, "shiftNumber", where)
    return ShiftRow(
        bounded_int(SOURCE, row, "playerId", where),
        team,
        period,
        game_second(period, start),
        game_second(period, end),
        number,
    )


def parse_shifts(payload: object, game_id: int) -> ShiftRows:
    """Return the shifts of one game from a shift chart payload.

    A row of another game, a team code that the maps do not know, a clock that is not ``MM:SS``,
    and a chart with no shift raise a named error. An interval with no length is kept: the stint
    step drops it and records it, because a parser must not decide what a feed fault means.
    """
    top = as_mapping(SOURCE, payload, "the shift chart")
    data = as_list(SOURCE, field(SOURCE, top, "data", "the shift chart"), "the shift chart data")
    shifts = tuple(s for s in (_parse_shift(raw, game_id) for raw in data) if s is not None)
    if not shifts:
        raise SourceParseError(
            SOURCE, f"shift chart of game {game_id} lists no shift; the chart may not exist yet"
        )
    return ShiftRows(game_id, shifts, len(data))


def shifts_url(game_id: int) -> str:
    """Return the URL of the shift chart of one game."""
    return f"{SHIFTS_URL}?cayenneExp=gameId={game_id}"


def fetch_shifts(client: HttpClient, game_id: int) -> ShiftRows:
    """Fetch and parse the shift chart of one game."""
    return parse_shifts(get_json(client, SOURCE, shifts_url(game_id)), game_id)
