"""Tests for the Daily Faceoff parser. It reads a fixture and makes no network request."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hockey_model.game import StarterSource, StarterStatus
from hockey_model.sources.daily_faceoff import STARTERS_URL, fetch_starters, parse_starters
from hockey_model.sources.http import HttpClient
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.sources.teams import NHL_TEAM_CODES, from_daily_faceoff

FIXTURE = Path(__file__).parent.parent / "fixtures" / "daily_faceoff" / "starting_goalies.html"
_SCRIPT = re.compile(r'(<script[^>]*id="__NEXT_DATA__"[^>]*>)(.*?)(</script>)', re.DOTALL)


def page_with(edit) -> str:
    """Return the fixture page after ``edit`` changed the parsed page data."""
    html = FIXTURE.read_text()
    match = _SCRIPT.search(html)
    assert match is not None
    data = json.loads(match.group(2))
    edit(data["props"]["pageProps"]["data"])
    return html[: match.start(2)] + json.dumps(data) + html[match.end(2) :]


def test_parse_starters_reads_both_sides() -> None:
    reads = parse_starters(FIXTURE.read_text())
    assert len(reads) == 22
    assert all(r.source is StarterSource.DAILY_FACEOFF for r in reads)
    pit = next(r for r in reads if r.team == "PIT")
    assert pit.goalie_name == "Arturs Silovs"
    assert pit.start == datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


def test_parse_starters_status_values() -> None:
    def edit(data: list[dict]) -> None:
        data[0]["homeNewsStrengthName"] = "Likely"
        data[0]["awayNewsStrengthName"] = "Confirmed"
        data[0]["homeGoalieName"] = "Home Goalie"

    reads = parse_starters(page_with(edit))
    by_name = {r.goalie_name: r.status for r in reads}
    assert by_name["Home Goalie"] is StarterStatus.LIKELY
    assert by_name["Carter Hart"] is StarterStatus.CONFIRMED
    assert StarterStatus.UNKNOWN in by_name.values()


def test_parse_starters_confirmed_in_fixture() -> None:
    reads = parse_starters(FIXTURE.read_text())
    assert sum(r.status is StarterStatus.CONFIRMED for r in reads) == 2


def test_parse_starters_reads_unknown_status_with_a_note() -> None:
    def edit(data: list[dict]) -> None:
        data[0]["homeGoalieName"] = "Home Goalie"
        data[0]["homeNewsStrengthName"] = "Rumoured"

    notes: list[str] = []
    reads = parse_starters(page_with(edit), notes)
    # One unnamed status must not drop the other 21 starters on the page (#325).
    assert len(reads) == 22
    home = next(read for read in reads if read.goalie_name == "Home Goalie")
    assert home.status is StarterStatus.UNKNOWN
    assert len(notes) == 1
    assert "Rumoured" in notes[0]
    assert "Home Goalie" in notes[0]


def test_parse_starters_skips_a_side_with_no_goalie() -> None:
    def edit(data: list[dict]) -> None:
        data[0]["awayGoalieName"] = None

    assert len(parse_starters(page_with(edit))) == 21


def test_parse_starters_rejects_non_string_goalie_name() -> None:
    def edit(data: list[dict]) -> None:
        data[0]["homeGoalieName"] = 12

    with pytest.raises(SourceParseError, match="homeGoalieName"):
        parse_starters(page_with(edit))


def test_parse_starters_rejects_unknown_team() -> None:
    def edit(data: list[dict]) -> None:
        data[0]["homeTeamAbbreviation"] = "ZZZ"

    with pytest.raises(SourceShapeError, match="ZZZ"):
        parse_starters(page_with(edit))


def test_parse_starters_rejects_page_without_next_data() -> None:
    with pytest.raises(SourceParseError, match="__NEXT_DATA__"):
        parse_starters("<html><body>maintenance</body></html>")


def test_parse_starters_rejects_bad_json_in_script() -> None:
    html = '<script id="__NEXT_DATA__" type="application/json">{oops</script>'
    with pytest.raises(SourceParseError, match="not valid JSON"):
        parse_starters(html)


def test_parse_starters_rejects_missing_data_key() -> None:
    html = '<script id="__NEXT_DATA__">{"props": {"pageProps": {}}}</script>'
    with pytest.raises(SourceParseError, match="'data'"):
        parse_starters(html)


def test_from_daily_faceoff_accepts_every_fixture_team() -> None:
    for read in parse_starters(FIXTURE.read_text()):
        assert read.team in NHL_TEAM_CODES
        assert from_daily_faceoff(read.team) == read.team


def test_from_daily_faceoff_rejects_unknown_code() -> None:
    with pytest.raises(SourceShapeError, match="XXX"):
        from_daily_faceoff("XXX")


class FakeClient(HttpClient):
    def __init__(self, body: bytes) -> None:
        super().__init__()
        self.body = body
        self.urls: list[str] = []

    def get(self, source: str, url: str) -> bytes:
        self.urls.append(url)
        return self.body


def test_fetch_starters_reads_the_starters_page() -> None:
    client = FakeClient(FIXTURE.read_bytes())
    assert len(fetch_starters(client)) == 22
    assert client.urls == [STARTERS_URL]


def test_fetch_starters_rejects_non_utf8() -> None:
    with pytest.raises(SourceParseError, match="UTF-8"):
        fetch_starters(FakeClient(b"\xff\xfe"))


@pytest.mark.parametrize("key", ["homeGoalieName", "awayGoalieName", "homeNewsStrengthName"])
def test_parse_starters_rejects_a_renamed_key(key: str) -> None:
    def edit(data: list[dict]) -> None:
        data[0][key + "X"] = data[0].pop(key)

    with pytest.raises(SourceParseError, match=key):
        parse_starters(page_with(edit))


def test_parse_starters_rejects_a_missing_team_abbreviation() -> None:
    def edit(data: list[dict]) -> None:
        del data[0]["homeTeamAbbreviation"]

    with pytest.raises(SourceParseError, match="homeTeamAbbreviation"):
        parse_starters(page_with(edit))
