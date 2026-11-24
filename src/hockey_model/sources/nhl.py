"""Read NHL API score days and boxscores. This module fetches and parses; it stores nothing."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import NamedTuple

from hockey_model.sources.http import HttpClient
from hockey_model.sources.payload import (
    as_list,
    as_mapping,
    field,
    get_json,
    int_field,
    str_field,
    utc_field,
)
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.team_codes import NHL_TEAM_CODES

SOURCE = "nhl"
NHL_BASE = "https://api-web.nhle.com/v1"
# The parser name that an archived payload records. Change the version when a parser change can
# change a parsed row.
SCORE_PARSER = "nhl_score:1"
BOXSCORE_PARSER = "nhl_boxscore:1"
# Regular season and playoffs. Preseason (1), All-Star (4), and national teams (9, 19) are out.
GAME_TYPES_KEPT = frozenset({2, 3})
FINAL_STATES = frozenset({"FINAL", "OFF"})
LAST_PERIODS = frozenset({"REG", "OT", "SO"})
# ASCII digits only, so a Unicode digit never parses as a time on ice.
_TOI = re.compile(r"([0-9]{1,3}):([0-9]{2})")


@dataclass(frozen=True, slots=True)
class GameRow:
    """One game as the NHL score feed reported it at one read."""

    game_id: int
    season: int
    game_type: int
    start: datetime
    home: str
    away: str
    state: str
    home_score: int | None
    away_score: int | None
    last_period: str | None

    @property
    def is_final(self) -> bool:
        """True when the NHL reports the game as over."""
        return self.state in FINAL_STATES


class PlayerRow(NamedTuple):
    """A player named in a boxscore."""

    nhl_id: int
    full_name: str
    position: str
    team: str


class SkaterRow(NamedTuple):
    """One skater line of a final boxscore."""

    team: str
    nhl_id: int
    toi_seconds: int
    goals: int
    assists: int


class GoalieRow(NamedTuple):
    """One goalie line of a final boxscore."""

    team: str
    nhl_id: int
    toi_seconds: int
    started: bool


@dataclass(frozen=True, slots=True)
class BoxscoreRows:
    """The players, skater lines, and goalie lines of one final game.

    ``home`` and ``away`` are the teams the boxscore declares, not teams read off the rows.
    """

    game_id: int
    home: str
    away: str
    players: list[PlayerRow]
    skaters: list[SkaterRow]
    goalies: list[GoalieRow]


def _team_code(team: Mapping[str, object], where: str) -> str:
    code = str_field(SOURCE, team, "abbrev", where)
    if code not in NHL_TEAM_CODES:
        raise SourceShapeError(SOURCE, code)
    return code


def _final_score(team: Mapping[str, object], where: str) -> int:
    return int_field(SOURCE, team, "score", where)


def _parse_game(raw: object) -> GameRow | None:
    game = as_mapping(SOURCE, raw, "a score day game")
    game_id = int_field(SOURCE, game, "id", "a score day game")
    where = f"game {game_id}"
    game_type = int_field(SOURCE, game, "gameType", where)
    if game_type not in GAME_TYPES_KEPT:
        return None
    home = as_mapping(SOURCE, field(SOURCE, game, "homeTeam", where), f"{where} homeTeam")
    away = as_mapping(SOURCE, field(SOURCE, game, "awayTeam", where), f"{where} awayTeam")
    state = str_field(SOURCE, game, "gameState", where)
    row = GameRow(
        game_id=game_id,
        season=int_field(SOURCE, game, "season", where),
        game_type=game_type,
        start=utc_field(SOURCE, game, "startTimeUTC", where),
        home=_team_code(home, f"{where} homeTeam"),
        away=_team_code(away, f"{where} awayTeam"),
        state=state,
        home_score=None,
        away_score=None,
        last_period=None,
    )
    if not row.is_final:
        return row
    return _with_result(row, game, home, away)


def _with_result(
    row: GameRow, game: Mapping[str, object], home: Mapping[str, object], away: Mapping[str, object]
) -> GameRow:
    where = f"final game {row.game_id}"
    outcome = as_mapping(SOURCE, field(SOURCE, game, "gameOutcome", where), f"{where} gameOutcome")
    last = str_field(SOURCE, outcome, "lastPeriodType", f"{where} gameOutcome")
    if last not in LAST_PERIODS:
        raise SourceParseError(SOURCE, f"{where} has unknown lastPeriodType {last!r}")
    return GameRow(
        game_id=row.game_id,
        season=row.season,
        game_type=row.game_type,
        start=row.start,
        home=row.home,
        away=row.away,
        state=row.state,
        home_score=_final_score(home, f"{where} homeTeam"),
        away_score=_final_score(away, f"{where} awayTeam"),
        last_period=last,
    )


def parse_score_day(payload: object) -> list[GameRow]:
    """Return the regular-season and playoff games of one score day.

    A game that is not final carries no scores, so it is never read as a result.
    """
    top = as_mapping(SOURCE, payload, "the score day")
    games = as_list(SOURCE, field(SOURCE, top, "games", "the score day"), "score day games")
    rows = (_parse_game(raw) for raw in games)
    return [row for row in rows if row is not None]


def _toi_seconds(value: object, where: str) -> int:
    match = _TOI.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise SourceParseError(SOURCE, f"{where} time on ice must be MM:SS, got {value!r}")
    minutes, seconds = int(match.group(1)), int(match.group(2))
    if seconds >= 60:
        raise SourceParseError(SOURCE, f"{where} time on ice must be MM:SS, got {value!r}")
    return minutes * 60 + seconds


def _player_name(player: Mapping[str, object], where: str) -> str:
    name = as_mapping(SOURCE, field(SOURCE, player, "name", where), f"{where} name")
    return str_field(SOURCE, name, "default", f"{where} name")


def _parse_side(side: Mapping[str, object], team: str, out: BoxscoreRows) -> None:
    where = f"{team} boxscore"
    for group in ("forwards", "defense"):
        for raw in as_list(SOURCE, field(SOURCE, side, group, where), f"{where} {group}"):
            player = as_mapping(SOURCE, raw, f"{where} {group} entry")
            nhl_id = int_field(SOURCE, player, "playerId", f"{where} {group} entry")
            label = f"{where} player {nhl_id}"
            out.players.append(
                PlayerRow(
                    nhl_id,
                    _player_name(player, label),
                    str_field(SOURCE, player, "position", label),
                    team,
                )
            )
            out.skaters.append(
                SkaterRow(
                    team,
                    nhl_id,
                    _toi_seconds(field(SOURCE, player, "toi", label), label),
                    int_field(SOURCE, player, "goals", label),
                    int_field(SOURCE, player, "assists", label),
                )
            )
    _parse_goalies(side, team, out)


def _parse_goalies(side: Mapping[str, object], team: str, out: BoxscoreRows) -> None:
    where = f"{team} boxscore"
    for raw in as_list(SOURCE, field(SOURCE, side, "goalies", where), f"{where} goalies"):
        goalie = as_mapping(SOURCE, raw, f"{where} goalie entry")
        nhl_id = int_field(SOURCE, goalie, "playerId", f"{where} goalie entry")
        label = f"{where} goalie {nhl_id}"
        started = field(SOURCE, goalie, "starter", label)
        if not isinstance(started, bool):
            raise SourceParseError(SOURCE, f"{label} field 'starter' must be true or false")
        out.players.append(PlayerRow(nhl_id, _player_name(goalie, label), "G", team))
        out.goalies.append(
            GoalieRow(
                team, nhl_id, _toi_seconds(field(SOURCE, goalie, "toi", label), label), started
            )
        )


def _check_sides(rows: BoxscoreRows, where: str) -> None:
    """Raise unless both declared teams have skater lines and one starting goalie.

    The teams come from the boxscore header, so a side with no rows at all cannot pass.
    """
    for team in (rows.home, rows.away):
        if not any(s.team == team for s in rows.skaters):
            raise SourceParseError(SOURCE, f"{where} has no skater rows for {team}")
        count = sum(1 for g in rows.goalies if g.team == team and g.started)
        if count != 1:
            raise SourceParseError(
                SOURCE, f"{where} has {count} starting goalies for {team}; expected 1"
            )


def parse_boxscore(payload: object) -> BoxscoreRows:
    """Return the player and stat rows of a final boxscore.

    A boxscore of a game that is not final raises, so a stat from a game in
    progress is never stored as a final stat.
    """
    top = as_mapping(SOURCE, payload, "the boxscore")
    game_id = int_field(SOURCE, top, "id", "the boxscore")
    where = f"boxscore {game_id}"
    state = str_field(SOURCE, top, "gameState", where)
    if state not in FINAL_STATES:
        raise SourceParseError(SOURCE, f"{where} is not final (state {state!r}); read it later")
    stats = as_mapping(SOURCE, field(SOURCE, top, "playerByGameStats", where), f"{where} stats")
    codes = {
        key: _team_code(
            as_mapping(SOURCE, field(SOURCE, top, key, where), f"{where} {key}"), f"{where} {key}"
        )
        for key in ("homeTeam", "awayTeam")
    }
    if codes["homeTeam"] == codes["awayTeam"]:
        raise SourceParseError(SOURCE, f"{where} names {codes['homeTeam']} as both teams")
    out = BoxscoreRows(game_id, codes["homeTeam"], codes["awayTeam"], [], [], [])
    for key, code in codes.items():
        side = as_mapping(SOURCE, field(SOURCE, stats, key, where), f"{where} stats {key}")
        _parse_side(side, code, out)
    _check_sides(out, where)
    return out


def score_day_url(day: date) -> str:
    """Return the URL of the NHL score feed for one calendar day."""
    return f"{NHL_BASE}/score/{day.isoformat()}"


def fetch_score_day(client: HttpClient, day: date) -> list[GameRow]:
    """Fetch and parse the NHL score feed for one calendar day."""
    return parse_score_day(get_json(client, SOURCE, score_day_url(day)))


def boxscore_url(game_id: int) -> str:
    """Return the URL of the boxscore of one game."""
    return f"{NHL_BASE}/gamecenter/{game_id}/boxscore"


def fetch_boxscore(client: HttpClient, game_id: int) -> BoxscoreRows:
    """Fetch and parse the boxscore of one final game."""
    rows = parse_boxscore(get_json(client, SOURCE, boxscore_url(game_id)))
    if rows.game_id != game_id:
        raise SourceParseError(SOURCE, f"boxscore for game {game_id} names game {rows.game_id}")
    return rows
