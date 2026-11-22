"""Read ESPN starting goalies and injuries. This module fetches and parses; it stores nothing.

ESPN does not support these endpoints. Fetch sparingly. Read no odds field.
"""

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
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
PROBABLE_GOALIE = "probableStartingGoalie"
# ESPN writes the morning status as "Expected" and the confirmed status as "Confirmed".
_STATUS_MAP = {"Confirmed": StarterStatus.CONFIRMED, "Expected": StarterStatus.LIKELY}
OUT_STATUSES = frozenset({"Out", "Injured Reserve", "Long Term Injured Reserve"})
_KNOWN_INJURY_STATUSES = OUT_STATUSES | {"Day-To-Day"}


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
        return self.status in OUT_STATUSES


def _team_code(competitor: Mapping[str, object], where: str) -> str:
    team = as_mapping(SOURCE, field(SOURCE, competitor, "team", where), f"{where} team")
    return from_espn(str_field(SOURCE, team, "abbreviation", f"{where} team"))


def _starter(probable: Mapping[str, object], event_start_where: str) -> tuple[str, StarterStatus]:
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
    if label not in _STATUS_MAP:
        raise SourceParseError(
            SOURCE, f"{event_start_where} has unknown goalie status {label!r}; add it to espn.py"
        )
    return name, _STATUS_MAP[label]


def _event_reads(event: Mapping[str, object]) -> tuple[list[StarterRead], int, int]:
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
            name, goalie_status = _starter(probable, f"{where} {team}")
            reads.append(StarterRead(start, team, name, goalie_status, StarterSource.ESPN))
    return reads, len(competitors), keyless


def parse_scoreboard_starters(payload: object) -> list[StarterRead]:
    """Return the probable starting goalies of the games that have not started.

    A page where no pregame competitor has a ``probables`` key raises: ESPN moved the key, and
    a page of zero reads would otherwise pass as a good read.
    """
    top = as_mapping(SOURCE, payload, "the scoreboard")
    events = as_list(SOURCE, field(SOURCE, top, "events", "the scoreboard"), "scoreboard events")
    reads: list[StarterRead] = []
    competitors = keyless = 0
    for raw in events:
        event_reads, event_competitors, event_keyless = _event_reads(
            as_mapping(SOURCE, raw, "a scoreboard event")
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


def _injury(raw: object) -> InjuryRead:
    entry = as_mapping(SOURCE, raw, "an injury entry")
    where = f"injury {entry.get('id', '?')}"
    athlete = as_mapping(SOURCE, field(SOURCE, entry, "athlete", where), f"{where} athlete")
    name = str_field(SOURCE, athlete, "displayName", f"{where} athlete")
    team = as_mapping(SOURCE, field(SOURCE, athlete, "team", where), f"{where} athlete team")
    status = str_field(SOURCE, entry, "status", where)
    if status not in _KNOWN_INJURY_STATUSES:
        raise SourceParseError(
            SOURCE, f"{where} has unknown injury status {status!r}; add it to espn.py"
        )
    code = from_espn(str_field(SOURCE, team, "abbreviation", f"{where} athlete team"))
    return InjuryRead(code, name, status, _return_date(entry, where))


def parse_injuries(payload: object) -> list[InjuryRead]:
    """Return every player on the injury list."""
    top = as_mapping(SOURCE, payload, "the injury list")
    clubs = as_list(SOURCE, field(SOURCE, top, "injuries", "the injury list"), "injury clubs")
    reads: list[InjuryRead] = []
    for raw in clubs:
        club = as_mapping(SOURCE, raw, "an injury club")
        entries = as_list(SOURCE, field(SOURCE, club, "injuries", "an injury club"), "club entries")
        reads.extend(_injury(entry) for entry in entries)
    return reads


def fetch_scoreboard_starters(client: HttpClient, day: date) -> list[StarterRead]:
    """Fetch the ESPN scoreboard for one day and return its probable starting goalies."""
    url = f"{ESPN_BASE}/scoreboard?dates={day:%Y%m%d}"
    return parse_scoreboard_starters(get_json(client, SOURCE, url))


def fetch_injuries(client: HttpClient) -> list[InjuryRead]:
    """Fetch the current ESPN injury list."""
    return parse_injuries(get_json(client, SOURCE, f"{ESPN_BASE}/injuries"))
