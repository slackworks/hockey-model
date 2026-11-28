"""Read ESPN starting goalies and injuries. This module fetches and parses; it stores nothing.

ESPN does not support these endpoints. Fetch sparingly. Read no odds field.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from hockey_model.game import StarterSource, StarterStatus
from hockey_model.sources.http import HttpClient
from hockey_model.sources.payload import (
    as_list,
    as_mapping,
    field,
    get_json,
    str_field,
    utc_field,
)
from hockey_model.sources.shape_errors import SourceParseError
from hockey_model.sources.starters import StarterRead
from hockey_model.sources.teams import from_espn

SOURCE = "espn"
# An ESPN event id is 1 to 12 ASCII digits.
EVENT_ID = re.compile(r"[0-9]{1,12}")
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
# The parser name that an archived payload records. Change the version when a parser change can
# change a parsed row. Both parsers took version 2 with the unnamed-status handling (#325).
SCOREBOARD_PARSER = "espn_scoreboard:2"
INJURIES_PARSER = "espn_injuries:2"
PROBABLE_GOALIE = "probableStartingGoalie"
# ESPN writes the morning status as "Expected" and the confirmed status as "Confirmed".
_STATUS_MAP = {"Confirmed": StarterStatus.CONFIRMED, "Expected": StarterStatus.LIKELY}
OUT_STATUSES = frozenset({"Out", "Injured Reserve", "Long Term Injured Reserve"})
# The one status that ESPN writes for a player who may still play. Every other status, known or
# not, counts as out, because a player whose status this code does not read may not play (#325).
NOT_OUT_STATUSES = frozenset({"Day-To-Day"})
KNOWN_INJURY_STATUSES = OUT_STATUSES | NOT_OUT_STATUSES


@dataclass(frozen=True, slots=True)
class InjuryRead:
    """One player on the ESPN injury list."""

    team: str
    player_name: str
    status: str
    return_date: date | None

    @property
    def is_out(self) -> bool:
        """True when the status means the player does not play."""
        return self.status not in NOT_OUT_STATUSES


def _team_code(competitor: Mapping[str, object], where: str) -> str:
    team = as_mapping(SOURCE, field(SOURCE, competitor, "team", where), f"{where} team")
    return from_espn(str_field(SOURCE, team, "abbreviation", f"{where} team"))


def _starter(
    probable: Mapping[str, object], event_start_where: str, notes: list[str]
) -> tuple[str, StarterStatus]:
    """Return the name and status of one probable starter.

    A status that this code does not name becomes ``UNKNOWN`` and adds a note, so one new label
    never fails the whole scoreboard read (#325). The read is kept and stored, and the model
    skips it, because an unnamed label is not a confidence that this code can act on.
    """
    athlete = as_mapping(
        SOURCE,
        field(SOURCE, probable, "athlete", event_start_where),
        f"{event_start_where} athlete",
    )
    name = str_field(SOURCE, athlete, "fullName", f"{event_start_where} athlete")
    status = as_mapping(
        SOURCE, field(SOURCE, probable, "status", event_start_where), f"{event_start_where} status"
    )
    label = str_field(SOURCE, status, "name", f"{event_start_where} status")
    if label in _STATUS_MAP:
        return name, _STATUS_MAP[label]
    notes.append(
        f"{SOURCE} starters: {name} ({event_start_where}) has status {label!r}, which is not named"
    )
    return name, StarterStatus.UNKNOWN


def _event_reads(
    event: Mapping[str, object], notes: list[str]
) -> tuple[list[StarterRead], int, int]:
    """Return the reads of one event, its pregame competitors, and those with no ``probables``."""
    where = f"event {event.get('id', '?')}"
    status = as_mapping(SOURCE, field(SOURCE, event, "status", where), f"{where} status")
    kind = as_mapping(SOURCE, field(SOURCE, status, "type", where), f"{where} status type")
    # ESPN overwrites the morning status once the game starts, so only a pregame read counts.
    if str_field(SOURCE, kind, "state", f"{where} status type") != "pre":
        return [], 0, 0
    start = utc_field(SOURCE, event, "date", where)
    competitions = as_list(SOURCE, field(SOURCE, event, "competitions", where), f"{where} games")
    if not competitions:
        raise SourceParseError(SOURCE, f"{where} has an empty 'competitions' list")
    competition = as_mapping(SOURCE, competitions[0], f"{where} game")
    reads: list[StarterRead] = []
    competitors = as_list(SOURCE, field(SOURCE, competition, "competitors", where), where)
    keyless = 0
    for raw in competitors:
        competitor = as_mapping(SOURCE, raw, f"{where} competitor")
        team = _team_code(competitor, where)
        # A missing key on one competitor reads as no goalie (#70). A key that is present must be
        # a list. parse_scoreboard_starters refuses a page where no competitor has the key.
        keyless += "probables" not in competitor
        for item in as_list(SOURCE, competitor.get("probables", []), f"{where} {team} probables"):
            probable = as_mapping(SOURCE, item, f"{where} {team} probable")
            if field(SOURCE, probable, "name", f"{where} {team} probable") != PROBABLE_GOALIE:
                continue
            name, goalie_status = _starter(probable, f"{where} {team}", notes)
            reads.append(StarterRead(start, team, name, goalie_status, StarterSource.ESPN))
    return reads, len(competitors), keyless


def parse_scoreboard_starters(payload: object, notes: list[str] | None = None) -> list[StarterRead]:
    """Return the probable starting goalies of the games that have not started.

    ``notes`` collects one line for each status that this code does not name, so the caller
    can report it. A status never stops the read (#325).

    A page where no pregame competitor has a ``probables`` key raises: ESPN moved the key, and
    a page of zero reads would otherwise pass as a good read.
    """
    top = as_mapping(SOURCE, payload, "the scoreboard")
    events = as_list(SOURCE, field(SOURCE, top, "events", "the scoreboard"), "scoreboard events")
    reads: list[StarterRead] = []
    found: list[str] = []
    competitors = keyless = 0
    for raw in events:
        event_reads, event_competitors, event_keyless = _event_reads(
            as_mapping(SOURCE, raw, "a scoreboard event"), found
        )
        reads += event_reads
        competitors += event_competitors
        keyless += event_keyless
    if competitors and keyless == competitors:
        raise SourceParseError(
            SOURCE,
            f"none of the {competitors} pregame competitors on the scoreboard has a 'probables' "
            "key; ESPN may have moved the goalie list, so update espn.py",
        )
    if notes is not None:
        # The read succeeded, so a note never hides a failure. It only names an unknown status.
        notes.extend(found)
    return reads


def _return_date(entry: Mapping[str, object], where: str) -> date | None:
    details = entry.get("details")
    if details is None:
        return None
    text = as_mapping(SOURCE, details, f"{where} details").get("returnDate")
    if text is None:
        return None
    if not isinstance(text, str):
        raise SourceParseError(SOURCE, f"{where} returnDate {text!r} is not a date string")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SourceParseError(SOURCE, f"{where} returnDate {text!r} is not a date") from exc


def _injury_with_note(raw: object) -> tuple[InjuryRead, str | None]:
    """Return one injury read, and a note when its status is one that this code does not name."""
    entry = as_mapping(SOURCE, raw, "an injury entry")
    where = f"injury {entry.get('id', '?')}"
    athlete = as_mapping(SOURCE, field(SOURCE, entry, "athlete", where), f"{where} athlete")
    name = str_field(SOURCE, athlete, "displayName", f"{where} athlete")
    team = as_mapping(SOURCE, field(SOURCE, athlete, "team", where), f"{where} athlete team")
    status = str_field(SOURCE, entry, "status", where)
    code = from_espn(str_field(SOURCE, team, "abbreviation", f"{where} athlete team"))
    read = InjuryRead(code, name, status, _return_date(entry, where))
    if status in KNOWN_INJURY_STATUSES:
        return read, None
    # A status this code does not name must not fail the whole list, and it must reach a person.
    return read, f"{SOURCE} injuries: {name} ({code}) has status {status!r}, which is not named"


def parse_injuries(payload: object, notes: list[str] | None = None) -> list[InjuryRead]:
    """Return every player on the injury list.

    ``notes`` collects one line for each status that this code does not name, so the caller
    can report it. A status never stops the read (#325).
    """
    top = as_mapping(SOURCE, payload, "the injury list")
    clubs = as_list(SOURCE, field(SOURCE, top, "injuries", "the injury list"), "injury clubs")
    reads: list[InjuryRead] = []
    found: list[str] = []
    for raw in clubs:
        club = as_mapping(SOURCE, raw, "an injury club")
        entries = as_list(SOURCE, field(SOURCE, club, "injuries", "an injury club"), "club entries")
        for entry in entries:
            read, note = _injury_with_note(entry)
            reads.append(read)
            if note is not None:
                found.append(note)
    if notes is not None:
        # The read succeeded, so a note never hides a failure. It only names an unknown status.
        notes.extend(found)
    return reads


def fetch_scoreboard_starters(
    client: HttpClient, day: date, notes: list[str] | None = None
) -> list[StarterRead]:
    """Fetch the ESPN scoreboard for one day and return its probable starting goalies.

    ``notes`` collects one line for each status that this code does not name, so the caller
    can report it. A status never stops the read (#325).
    """
    url = f"{ESPN_BASE}/scoreboard?dates={day:%Y%m%d}"
    return parse_scoreboard_starters(get_json(client, SOURCE, url), notes)


def fetch_injuries(client: HttpClient, notes: list[str] | None = None) -> list[InjuryRead]:
    """Fetch the current ESPN injury list.

    ``notes`` collects one line for each status that this code does not name, so the caller
    can report it. A status never stops the read (#325).
    """
    return parse_injuries(get_json(client, SOURCE, f"{ESPN_BASE}/injuries"), notes)
