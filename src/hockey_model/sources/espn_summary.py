"""Read the ESPN game summary of one game. This module fetches and parses; it stores nothing.

ESPN is the independent check of the NHL shift chart: it gives the time on ice of each skater
split by strength, and the number of shifts. The payload also holds betting-market keys. The
archive removes them before the hash (``sources/sanitize.py``), and this parser reads none.
Design: docs/design/pbp-shift-dataset.md
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl_events import clock_seconds
from hockey_model.sources.payload import (
    as_list,
    as_mapping,
    field,
    get_json,
    str_field,
)
from hockey_model.sources.shape_errors import SourceParseError
from hockey_model.sources.teams import from_espn

SOURCE = "espn_summary"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary"
# Change the version when a parser change can change a parsed row.
SUMMARY_PARSER = "espn_summary:1"
# The groups of skaters in ``boxscore.players``. The goalies sit in their own group.
SKATER_GROUPS = frozenset({"forwards", "defenses", "skaters"})
# The stat keys that the reconciliation reads.
_STAT_KEYS = (
    "timeOnIce",
    "powerPlayTimeOnIce",
    "shortHandedTimeOnIce",
    "evenStrengthTimeOnIce",
    "shifts",
)
# The longest time on ice that a skater can play: more than the longest game on record.
MAX_TOI_SECONDS = 7200
# ASCII digits only, so a Unicode digit never parses as a number.
_COUNT = re.compile(r"[0-9]{1,4}")
_EVENT_ID = re.compile(r"[0-9]{1,12}")


@dataclass(frozen=True, slots=True)
class EspnSkaterToi:
    """The time on ice of one skater, in seconds, split by strength."""

    team: str
    name: str
    toi: int
    pp_toi: int
    sh_toi: int
    es_toi: int
    shifts: int


@dataclass(frozen=True, slots=True)
class EspnSummary:
    """The skater lines of one ESPN game summary."""

    skaters: tuple[EspnSkaterToi, ...]


def _seconds(value: object, where: str) -> int:
    return clock_seconds(value, f"{SOURCE} {where}", limit=MAX_TOI_SECONDS)


def _count(value: object, where: str) -> int:
    if not isinstance(value, str) or _COUNT.fullmatch(value) is None:
        raise SourceParseError(SOURCE, f"{where} count must be digits, got {value!r}")
    return int(value)


def _stat_columns(group: Mapping[str, object], where: str) -> dict[str, int]:
    keys = as_list(SOURCE, field(SOURCE, group, "keys", where), f"{where} keys")
    names = [key for key in keys if isinstance(key, str)]
    if len(names) != len(keys) or len(set(names)) != len(names):
        raise SourceParseError(SOURCE, f"{where} keys must be unique strings")
    columns = {key: index for index, key in enumerate(names)}
    missing = [key for key in _STAT_KEYS if key not in columns]
    if missing:
        raise SourceParseError(SOURCE, f"{where} has no stat column {', '.join(missing)}")
    return columns


def _skater(raw: object, columns: Mapping[str, int], team: str, where: str) -> EspnSkaterToi:
    entry = as_mapping(SOURCE, raw, f"{where} athlete")
    athlete = as_mapping(SOURCE, field(SOURCE, entry, "athlete", where), f"{where} athlete")
    name = str_field(SOURCE, athlete, "displayName", f"{where} athlete")
    stats = as_list(SOURCE, field(SOURCE, entry, "stats", f"{where} {name}"), f"{name} stats")

    def stat(key: str) -> object:
        index = columns[key]
        if index >= len(stats):
            raise SourceParseError(SOURCE, f"{where} {name} has no value for {key!r}")
        return stats[index]

    return EspnSkaterToi(
        team,
        name,
        _seconds(stat("timeOnIce"), f"{name} timeOnIce"),
        _seconds(stat("powerPlayTimeOnIce"), f"{name} powerPlayTimeOnIce"),
        _seconds(stat("shortHandedTimeOnIce"), f"{name} shortHandedTimeOnIce"),
        _seconds(stat("evenStrengthTimeOnIce"), f"{name} evenStrengthTimeOnIce"),
        _count(stat("shifts"), f"{name} shifts"),
    )


def _team_skaters(side: object, where: str) -> list[EspnSkaterToi]:
    entry = as_mapping(SOURCE, side, f"{where} team entry")
    team_info = as_mapping(SOURCE, field(SOURCE, entry, "team", where), f"{where} team")
    team = from_espn(str_field(SOURCE, team_info, "abbreviation", f"{where} team"))
    skaters: list[EspnSkaterToi] = []
    groups = as_list(SOURCE, field(SOURCE, entry, "statistics", where), f"{where} statistics")
    for raw in groups:
        group = as_mapping(SOURCE, raw, f"{where} stat group")
        if group.get("name") not in SKATER_GROUPS:
            continue
        columns = _stat_columns(group, f"{where} {group.get('name')}")
        athletes = as_list(SOURCE, field(SOURCE, group, "athletes", where), f"{where} athletes")
        skaters.extend(_skater(a, columns, team, f"{where} {team}") for a in athletes)
    return skaters


def parse_summary(payload: object, event_id: str) -> EspnSummary:
    """Return the skater time on ice of the ESPN summary of event ``event_id``.

    A missing stat column, a time that is not ``M:SS``, a team code that ESPN writes and the maps
    do not know, a payload for another event, a skater listed twice, and a summary that does not
    list two teams raise a named error.
    """
    top = as_mapping(SOURCE, payload, "the summary")
    where = f"summary {event_id}"
    header = as_mapping(SOURCE, field(SOURCE, top, "header", where), f"{where} header")
    named = str(field(SOURCE, header, "id", f"{where} header"))
    if named != event_id:
        raise SourceParseError(SOURCE, f"{where} names event {named}")
    box = as_mapping(SOURCE, field(SOURCE, top, "boxscore", where), f"{where} boxscore")
    sides = as_list(SOURCE, field(SOURCE, box, "players", where), f"{where} boxscore players")
    skaters = [s for side in sides for s in _team_skaters(side, where)]
    if not skaters:
        raise SourceParseError(SOURCE, f"{where} lists no skater")
    if len({s.team for s in skaters}) != 2:
        raise SourceParseError(SOURCE, f"{where} must list skaters of two teams")
    keys = [(s.team, s.name) for s in skaters]
    if len(set(keys)) != len(keys):
        raise SourceParseError(SOURCE, f"{where} lists a skater twice for one team")
    return EspnSummary(tuple(skaters))


def summary_url(event_id: str) -> str:
    """Return the URL of the ESPN summary of one event, or raise for an id that is not digits."""
    if _EVENT_ID.fullmatch(event_id) is None:
        raise SourceParseError(
            SOURCE, f"ESPN event id must be 1 to 12 ASCII digits, got {event_id!r}"
        )
    return f"{SUMMARY_URL}?event={event_id}"


def fetch_summary(client: HttpClient, event_id: str) -> EspnSummary:
    """Fetch and parse the ESPN summary of one event."""
    return parse_summary(get_json(client, SOURCE, summary_url(event_id)), event_id)
