"""Read Daily Faceoff starting goalies. This module fetches and parses; it stores nothing.

Daily Faceoff has no API. The page holds its data as JSON in a ``__NEXT_DATA__`` script.
"""

import re
from collections.abc import Mapping

from hockey_model.game import StarterSource, StarterStatus
from hockey_model.sources.http import HttpClient
from hockey_model.sources.payload import as_list, as_mapping, field, get_json_text, utc_field
from hockey_model.sources.shape_errors import SourceParseError
from hockey_model.sources.starters import StarterRead
from hockey_model.sources.teams import from_daily_faceoff

SOURCE = "daily_faceoff"
STARTERS_URL = "https://www.dailyfaceoff.com/starting-goalies"
# The parser name that an archived payload records. Change the version when a parser change can
# change a parsed row.
PARSER = "daily_faceoff:1"
_NEXT_DATA = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
# A goalie that the page names without a strength is the page's projection only.
_STATUS_MAP = {
    None: StarterStatus.UNKNOWN,
    "Likely": StarterStatus.LIKELY,
    "Confirmed": StarterStatus.CONFIRMED,
}
_SIDES = ("home", "away")


def _page_games(html: str) -> list[object]:
    match = _NEXT_DATA.search(html)
    if match is None:
        raise SourceParseError(SOURCE, "the page has no __NEXT_DATA__ script; the page changed")
    top = as_mapping(
        SOURCE, get_json_text(SOURCE, match.group(1), "__NEXT_DATA__"), "__NEXT_DATA__"
    )
    props = as_mapping(SOURCE, field(SOURCE, top, "props", "__NEXT_DATA__"), "props")
    page = as_mapping(SOURCE, field(SOURCE, props, "pageProps", "props"), "pageProps")
    return as_list(SOURCE, field(SOURCE, page, "data", "pageProps"), "pageProps data")


def _side_read(game: Mapping[str, object], side: str, where: str) -> StarterRead | None:
    name = field(SOURCE, game, f"{side}GoalieName", where)
    label = field(SOURCE, game, f"{side}NewsStrengthName", where)
    if name is None:
        return None
    if not isinstance(name, str) or not name:
        raise SourceParseError(SOURCE, f"{where} {side}GoalieName must be a string, got {name!r}")
    status = _STATUS_MAP.get(label) if label is None or isinstance(label, str) else None
    if status is None:
        raise SourceParseError(
            SOURCE, f"{where} has unknown goalie status {label!r}; add it to daily_faceoff.py"
        )
    abbrev = field(SOURCE, game, f"{side}TeamAbbreviation", where)
    if not isinstance(abbrev, str):
        raise SourceParseError(SOURCE, f"{where} {side}TeamAbbreviation must be a string")
    return StarterRead(
        utc_field(SOURCE, game, "dateGmt", where),
        from_daily_faceoff(abbrev),
        name,
        status,
        StarterSource.DAILY_FACEOFF,
    )


def parse_starters(html: str) -> list[StarterRead]:
    """Return the starting goalie reads of the page. A side with no goalie name is skipped."""
    reads: list[StarterRead] = []
    for raw in _page_games(html):
        game = as_mapping(SOURCE, raw, "a page game")
        where = f"game at {game.get('dateGmt', '?')}"
        for side in _SIDES:
            read = _side_read(game, side, where)
            if read is not None:
                reads.append(read)
    return reads


def fetch_starters(client: HttpClient) -> list[StarterRead]:
    """Fetch the Daily Faceoff starting goalies page and parse it."""
    try:
        html = client.get(SOURCE, STARTERS_URL).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceParseError(SOURCE, f"{STARTERS_URL} is not UTF-8 text: {exc}") from exc
    return parse_starters(html)
