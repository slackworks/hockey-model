"""Shared helpers for the refresh tests: a fake client that serves the fixtures by URL."""

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count as count_from
from pathlib import Path

from hockey_model.sources.errors import SourceError
from hockey_model.sources.http import HttpClient
from hockey_model.sources.moneypuck import TEAM_GAMES_URL

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
SCORE_URL = "https://api-web.nhle.com/v1/score/"
BOX_URL = "https://api-web.nhle.com/v1/gamecenter/"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


MAIN_GAME = 2025020697
SCORE_TEAMS = {
    game["id"]: (game["homeTeam"]["abbrev"], game["awayTeam"]["abbrev"])
    for game in json.loads(fixture("nhl/score_2026-01-10.json"))["games"]
}


class FakeClient(HttpClient):
    """Serve the fixtures by URL and record every request."""

    def __init__(self, overrides: dict[str, bytes | Exception] | None = None) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.overrides = overrides or {}

    def get(self, source: str, url: str) -> bytes:
        self.urls.append(url)
        body = self.overrides[url] if url in self.overrides else self._fixture_for(source, url)
        if isinstance(body, Exception):
            raise body
        return body

    def _fixture_for(self, source: str, url: str) -> bytes:
        if url.startswith(SCORE_URL):
            return self._score_day(url)
        if url.startswith(BOX_URL):
            return self._boxscore(int(url.split("/")[-2]))
        if url == TEAM_GAMES_URL:
            return fixture("moneypuck/all_teams_sample.csv")
        if "seasonPlayersSummary" in url:
            return fixture("moneypuck/goalies_2025_sample.zip")
        if "scoreboard" in url:
            return scoreboard(url.endswith("20260110")).encode()
        if url == f"{ESPN}/injuries":
            return fixture("espn/injuries.json")
        if "dailyfaceoff" in url:
            return fixture("daily_faceoff/starting_goalies.html")
        raise SourceError(source, url, "HTTP 404")

    @staticmethod
    def _boxscore(game_id: int) -> bytes:
        """The fixture boxscore, with the teams of the stored game as a real response has.

        Each game but the main one gets its own players, because one player is on one team
        in one read.
        """
        payload = json.loads(fixture("nhl/boxscore_2025020709.json"))
        payload["id"] = game_id
        if game_id in SCORE_TEAMS:
            payload["homeTeam"]["abbrev"], payload["awayTeam"]["abbrev"] = SCORE_TEAMS[game_id]
        if game_id != MAIN_GAME:
            for side in payload["playerByGameStats"].values():
                for group in side.values():
                    for entry in group:
                        entry["playerId"] += (game_id % 1000) * 100_000_000
                        entry["name"]["default"] += f" {game_id}"
        return json.dumps(payload).encode()

    @staticmethod
    def _score_day(url: str) -> bytes:
        if url.endswith("2026-01-10"):
            return fixture("nhl/score_2026-01-10.json")
        return b'{"games": []}'


def scoreboard(with_game: bool) -> str:
    """One ESPN game that matches stored game 2025020697: NYR at BOS, 2026-01-10 18:00Z."""
    payload = json.loads(fixture("espn/scoreboard_20261103.json"))
    event = copy.deepcopy(payload["events"][0])
    event["date"] = "2026-01-10T18:00Z"
    away, home = event["competitions"][0]["competitors"][0:2]
    away["team"]["abbreviation"], home["team"]["abbreviation"] = "NYR", "BOS"
    payload["events"] = [event] if with_game else []
    return json.dumps(payload)


def failing_days(*days: str) -> dict[str, bytes | Exception]:
    return {f"{SCORE_URL}{day}": SourceError("nhl", "u", "HTTP 503") for day in days}


def score_requests(client: FakeClient) -> list[str]:
    return [u[len(SCORE_URL) :] for u in client.urls if u.startswith(SCORE_URL)]


def minute_clock(origin: datetime = NOW) -> Callable[[], datetime]:
    ticks = count_from(0)
    return lambda: origin + timedelta(minutes=next(ticks))
