"""Read the events of one ESPN scoreboard day, to find the ESPN event of an NHL game (#35).

This module fetches and parses. It stores nothing. The scoreboard of a day is a market source:
the recording client removes its price fields before the bytes reach the archive. Design:
docs/design/pbp-shift-dataset.md
"""

from dataclasses import dataclass
from datetime import date, datetime

from hockey_model.sources.espn import ESPN_BASE, EVENT_ID, SOURCE
from hockey_model.sources.http import HttpClient
from hockey_model.sources.payload import (
    as_list,
    as_mapping,
    field,
    get_json,
    str_field,
    utc_field,
)
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.sources.teams import from_espn

EVENTS_PARSER = "espn_scoreboard_events:1"


@dataclass(frozen=True, slots=True)
class EspnEvent:
    """One ESPN game: its event id, its start, and the NHL codes of its two teams."""

    event_id: str
    start: datetime
    home: str
    away: str


def _team(competitor: object, where: str) -> tuple[str, str]:
    """Return the side (``home`` or ``away``) and the NHL code of one competitor."""
    item = as_mapping(SOURCE, competitor, f"{where} competitor")
    side = str_field(SOURCE, item, "homeAway", where)
    if side not in {"home", "away"}:
        raise SourceParseError(SOURCE, f"{where} has homeAway {side!r}; it must be home or away")
    team = as_mapping(SOURCE, field(SOURCE, item, "team", where), f"{where} team")
    return side, from_espn(str_field(SOURCE, team, "abbreviation", f"{where} team"))


def _event(raw: object) -> EspnEvent:
    item = as_mapping(SOURCE, raw, "scoreboard event")
    event_id = str(field(SOURCE, item, "id", "scoreboard event"))
    if EVENT_ID.fullmatch(event_id) is None:
        raise SourceParseError(SOURCE, f"event id {event_id!r} must be 1 to 12 ASCII digits")
    where = f"event {event_id}"
    start = utc_field(SOURCE, item, "date", where)
    games = as_list(SOURCE, field(SOURCE, item, "competitions", where), f"{where} games")
    if not games:
        raise SourceParseError(SOURCE, f"{where} has an empty 'competitions' list")
    game = as_mapping(SOURCE, games[0], f"{where} game")
    rows = as_list(SOURCE, field(SOURCE, game, "competitors", where), where)
    if len(rows) != 2:
        raise SourceParseError(SOURCE, f"{where} must list two competitors, got {len(rows)}")
    sides = dict(_team(row, where) for row in rows)
    if set(sides) != {"home", "away"}:
        raise SourceParseError(SOURCE, f"{where} must have one home and one away competitor")
    return EspnEvent(event_id, start, sides["home"], sides["away"])


def parse_scoreboard_events(payload: object, notes: list[str] | None = None) -> list[EspnEvent]:
    """Return the events of a scoreboard page, in the order of the page.

    A missing ``events`` list, an event id that is not digits, a missing date, a competitor
    count other than two, a side other than home and away, and an event that the page lists
    twice raise a named error. A day with no game gives an empty list. An event with a team code
    that the maps do not know, such as an all-star game, is skipped and named in ``notes``, so
    it does not hide the NHL games of the day.
    """
    top = as_mapping(SOURCE, payload, "the scoreboard")
    raw_events = as_list(SOURCE, field(SOURCE, top, "events", "scoreboard"), "events")
    events: list[EspnEvent] = []
    for raw in raw_events:
        try:
            events.append(_event(raw))
        except SourceShapeError as exc:
            if notes is not None:
                notes.append(f"{SOURCE} scoreboard: skipped an event: {exc}")
    ids = [event.event_id for event in events]
    if len(set(ids)) != len(ids):
        raise SourceParseError(SOURCE, "the scoreboard lists an event twice")
    return events


def scoreboard_url(day: date) -> str:
    """Return the URL of the ESPN scoreboard of one day."""
    return f"{ESPN_BASE}/scoreboard?dates={day:%Y%m%d}"


def fetch_scoreboard_events(
    client: HttpClient, day: date, notes: list[str] | None = None
) -> list[EspnEvent]:
    """Fetch and parse the ESPN scoreboard of one day. ``notes`` collects the skipped events."""
    return parse_scoreboard_events(get_json(client, SOURCE, scoreboard_url(day)), notes)
