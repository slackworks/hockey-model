"""Read the MoneyPuck game-by-game CSV files. This module fetches and parses; it stores nothing.

Data from MoneyPuck.com. MoneyPuck allows these files for non-commercial use and asks for
credit. Fetch only the URLs that ``check_allowed`` accepts.
"""

import csv
import io
import math
import re
import zipfile
from collections.abc import Iterator, Mapping
from datetime import date, timedelta
from typing import NamedTuple

from hockey_model.sources.errors import SourceError
from hockey_model.sources.http import HttpClient
from hockey_model.sources.shape_errors import SourceParseError
from hockey_model.sources.teams import from_moneypuck

SOURCE = "moneypuck"
# The parser names that an archived payload records. Change a version when a parser change can
# change a parsed row.
TEAM_PARSER = "moneypuck_team:1"
GOALIE_PARSER = "moneypuck_goalie:1"
MONEYPUCK_CREDIT = "Data from MoneyPuck.com."
TEAM_GAMES_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/all_teams.csv"
_GOALIES_URL = "https://peter-tanner.com/moneypuck/downloads/seasonPlayersSummary/goalies/{}.zip"
REFRESH_AFTER = timedelta(days=7)
# The Elo burn-in starts with the 2014-15 season. Older rows would need old team codes.
FIRST_SEASON = 2014
# A ZIP member that expands past this size is refused.
MAX_CSV_BYTES = 256 * 1024 * 1024
# MoneyPuck writes the whole-game rows with this situation. Other rows split by strength.
ALL_SITUATIONS = "all"
KEPT_GAME_TYPES = frozenset({"02", "03"})

TEAM_COLUMNS = (
    "team",
    "season",
    "gameId",
    "gameDate",
    "situation",
    "xGoalsFor",
    "xGoalsAgainst",
    "shotAttemptsFor",
    "shotAttemptsAgainst",
)
GOALIE_COLUMNS = ("playerId", "season", "gameId", "playerTeam", "situation", "xGoals", "goals")
_SEASON = re.compile(r"[0-9]{4}")
_GAME_ID = re.compile(r"[0-9]{10}")
# ASCII digits only: float() also takes "1_0", padded text, and non-ASCII digits.
_DECIMAL = re.compile(r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_GAME_DATE = re.compile(r"[0-9]{8}")
_ALLOWED_GOALIE_URL = re.compile(
    r"https://peter-tanner\.com/moneypuck/downloads/seasonPlayersSummary/goalies/[0-9]{4}\.zip"
)


class TeamGameRow(NamedTuple):
    """One team's side of one game: expected goals for and against."""

    game_id: int
    team: str
    game_date: str
    xg_for: float
    xg_against: float
    shot_attempts_for: int
    shot_attempts_against: int


class GoalieGameRow(NamedTuple):
    """One goalie's game: expected and actual goals against."""

    game_id: int
    nhl_id: int
    team: str
    xg_against: float
    goals_against: int


def goalie_games_url(season_start_year: int) -> str:
    """Return the goalie ZIP URL for the season that starts in ``season_start_year``."""
    return _GOALIES_URL.format(season_start_year)


def check_allowed(url: str) -> None:
    """Raise ``SourceError`` unless ``url`` is one of the MoneyPuck files the owner allows."""
    if url == TEAM_GAMES_URL or _ALLOWED_GOALIE_URL.fullmatch(url):
        return
    raise SourceError(
        SOURCE,
        url,
        "this MoneyPuck URL is not on the allowed list; fetch only the listed CSV and ZIP files",
        permanent=True,
    )


def _number(row: Mapping[str, str], column: str, line: int) -> float:
    text = row[column]
    value = float(text) if _DECIMAL.fullmatch(text) else math.nan
    if not math.isfinite(value) or value < 0.0:
        raise SourceParseError(
            SOURCE, f"line {line} column {column!r} must be a non-negative number, got {text!r}"
        )
    return value


def _count(row: Mapping[str, str], column: str, line: int) -> int:
    value = _number(row, column, line)
    if value != int(value):
        raise SourceParseError(
            SOURCE, f"line {line} column {column!r} must be a whole number, got {row[column]!r}"
        )
    return int(value)


def _check_columns(reader: csv.DictReader[str], required: tuple[str, ...]) -> None:
    try:
        names = set(reader.fieldnames or ())
    except csv.Error as exc:
        raise SourceParseError(SOURCE, f"the file is not valid CSV: {exc}") from exc
    missing = [column for column in required if column not in names]
    if missing:
        raise SourceParseError(
            SOURCE, f"the file has no column {missing[0]!r}; MoneyPuck changed its format"
        )


def _game_id(row: Mapping[str, str], line: int) -> int:
    text = row["gameId"]
    if not _GAME_ID.fullmatch(text) or text[:4] != row["season"]:
        raise SourceParseError(
            SOURCE,
            f"line {line} gameId {text!r} must be 10 digits that start with season "
            f"{row['season']!r}",
        )
    return int(text)


def _kept_rows(reader: csv.DictReader[str]) -> Iterator[tuple[int, Mapping[str, str]]]:
    """Yield the whole-game rows of the kept seasons and game types, with the line number."""
    for line, row in enumerate(reader, start=2):
        if None in row or None in row.values():
            raise SourceParseError(SOURCE, f"line {line} has a different number of fields")
        if row["situation"] != ALL_SITUATIONS:
            continue
        if not _SEASON.fullmatch(row["season"]) or int(row["season"]) < FIRST_SEASON:
            continue
        if row["gameId"][4:6] not in KEPT_GAME_TYPES:
            continue
        yield line, row


def _checked_rows(reader: csv.DictReader[str]) -> Iterator[tuple[int, Mapping[str, str]]]:
    """Yield the kept rows, and turn a CSV format error into a named parse error."""
    try:
        yield from _kept_rows(reader)
    except csv.Error as exc:
        raise SourceParseError(SOURCE, f"the file is not valid CSV: {exc}") from exc


def _game_date(text: str, line: int) -> str:
    if not _GAME_DATE.fullmatch(text):
        raise SourceParseError(SOURCE, f"line {line} gameDate must be YYYYMMDD, got {text!r}")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise SourceParseError(SOURCE, f"line {line} gameDate {text!r} is not a date") from exc


def parse_team_games(text: str) -> list[TeamGameRow]:
    """Return one row per team and game from ``all_teams.csv`` text."""
    reader = csv.DictReader(io.StringIO(text))
    _check_columns(reader, TEAM_COLUMNS)
    rows: list[TeamGameRow] = []
    seen: set[tuple[int, str]] = set()
    for line, row in _checked_rows(reader):
        game_id = _game_id(row, line)
        team = from_moneypuck(row["team"])
        if (game_id, team) in seen:
            raise SourceParseError(SOURCE, f"line {line} repeats game {game_id} for team {team}")
        seen.add((game_id, team))
        rows.append(
            TeamGameRow(
                game_id,
                team,
                _game_date(row["gameDate"], line),
                _number(row, "xGoalsFor", line),
                _number(row, "xGoalsAgainst", line),
                _count(row, "shotAttemptsFor", line),
                _count(row, "shotAttemptsAgainst", line),
            )
        )
    return rows


def _csv_text(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SourceParseError(SOURCE, f"the goalie file is not a ZIP archive: {exc}") from exc
    with archive:
        members = archive.infolist()
        if len(members) != 1:
            raise SourceParseError(
                SOURCE, f"the goalie ZIP must hold one file, found {len(members)}"
            )
        if members[0].file_size > MAX_CSV_BYTES:
            raise SourceParseError(
                SOURCE, f"the goalie file expands to {members[0].file_size} bytes; refusing it"
            )
        try:
            return archive.read(members[0]).decode("utf-8")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, UnicodeDecodeError) as exc:
            raise SourceParseError(SOURCE, f"the goalie file cannot be read: {exc}") from exc


def parse_goalie_games(data: bytes) -> list[GoalieGameRow]:
    """Return one row per goalie and game from a goalie ZIP."""
    reader = csv.DictReader(io.StringIO(_csv_text(data)))
    _check_columns(reader, GOALIE_COLUMNS)
    rows: list[GoalieGameRow] = []
    seen: set[tuple[int, int]] = set()
    for line, row in _checked_rows(reader):
        game_id = _game_id(row, line)
        nhl_id = _count(row, "playerId", line)
        if (game_id, nhl_id) in seen:
            raise SourceParseError(SOURCE, f"line {line} repeats game {game_id} goalie {nhl_id}")
        seen.add((game_id, nhl_id))
        rows.append(
            GoalieGameRow(
                game_id,
                nhl_id,
                from_moneypuck(row["playerTeam"]),
                _number(row, "xGoals", line),
                _count(row, "goals", line),
            )
        )
    return rows


def fetch_team_games(client: HttpClient) -> list[TeamGameRow]:
    """Download and parse ``all_teams.csv``."""
    check_allowed(TEAM_GAMES_URL)
    body = client.get(SOURCE, TEAM_GAMES_URL)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceParseError(SOURCE, f"the team file is not UTF-8 text: {exc}") from exc
    return parse_team_games(text)


def fetch_goalie_games(client: HttpClient, season_start_year: int) -> list[GoalieGameRow]:
    """Download and parse the goalie ZIP of one season."""
    url = goalie_games_url(season_start_year)
    check_allowed(url)
    return parse_goalie_games(client.get(SOURCE, url))
