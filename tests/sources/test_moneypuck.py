"""Tests for the MoneyPuck parsers. They read fixtures and make no network request."""

import csv
import io
import zipfile
from pathlib import Path

import pytest

from hockey_model.sources.errors import SourceError
from hockey_model.sources.http import HttpClient
from hockey_model.sources.moneypuck import (
    MONEYPUCK_CREDIT,
    TEAM_GAMES_URL,
    check_allowed,
    fetch_goalie_games,
    fetch_team_games,
    goalie_games_url,
    parse_goalie_games,
    parse_team_games,
)
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.sources.teams import from_moneypuck

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moneypuck"


def team_text() -> str:
    return (FIXTURES / "all_teams_sample.csv").read_text()


def edit_row(text: str, game_id: str, club: str, **changes: str) -> str:
    """Return ``text`` with columns changed on the whole-game row of one team and game."""
    lines = text.splitlines()
    header = lines[0].split(",")
    for index, line in enumerate(lines[1:], start=1):
        cells = line.split(",")
        if (cells[header.index("gameId")], cells[header.index("team")]) != (game_id, club):
            continue
        if cells[header.index("situation")] != "all":
            continue
        for column, value in changes.items():
            cells[header.index(column)] = value
        lines[index] = ",".join(cells)
        return "\n".join(lines)
    raise AssertionError(f"no row for {game_id} {club}")


def zip_of(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buf.getvalue()


def test_credit_text() -> None:
    assert MONEYPUCK_CREDIT == "Data from MoneyPuck.com."


@pytest.mark.parametrize(
    "url",
    [
        "https://moneypuck.com/teams.htm",
        "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/all_teams.csv?x=1",
        "https://moneypuck.com/moneypuck/playerData/seasonSummary/2025/regular/teams.csv",
        "https://peter-tanner.com/moneypuck/downloads/seasonPlayersSummary/goalies/20.zip",
        "https://peter-tanner.com/moneypuck/downloads/seasonPlayersSummary/skaters/2025.zip",
        "http://moneypuck.com/moneypuck/playerData/careers/gameByGame/all_teams.csv",
    ],
)
def test_check_allowed_rejects_other_urls(url: str) -> None:
    with pytest.raises(SourceError, match="allowed list") as info:
        check_allowed(url)
    # No retry can add the URL to the list, so the failure is permanent.
    assert info.value.permanent is True


def test_check_allowed_accepts_listed_urls() -> None:
    check_allowed(TEAM_GAMES_URL)
    check_allowed(goalie_games_url(2025))


def test_parse_team_games_keeps_all_situation_only() -> None:
    rows = parse_team_games(team_text())
    keys = [(r.game_id, r.team) for r in rows]
    assert len(keys) == len(set(keys))
    boston = next(r for r in rows if (r.game_id, r.team) == (2025020697, "BOS"))
    assert boston.xg_for > 0
    assert boston.xg_against > 0
    assert boston.game_date == "2026-01-10"


def test_parse_team_games_keeps_the_attempt_counts() -> None:
    rows = parse_team_games(team_text())
    boston = next(r for r in rows if (r.game_id, r.team) == (2025020697, "BOS"))
    assert boston.shot_attempts_for > 0
    assert boston.shot_attempts_against > 0


def test_parse_team_games_drops_seasons_before_2014() -> None:
    rows = parse_team_games(team_text())
    assert all(r.game_id >= 2014000000 for r in rows)
    assert 2013020001 not in {r.game_id for r in rows}


def test_parse_team_games_maps_team_codes() -> None:
    teams = {r.team for r in parse_team_games(team_text())}
    assert {"LAK", "NJD", "SJS", "ARI"} <= teams
    assert not {"L.A", "N.J", "S.J", "T.B"} & teams


def test_parse_team_games_keeps_playoff_games() -> None:
    assert any(str(r.game_id)[4:6] == "03" for r in parse_team_games(team_text()))


def test_moneypuck_game_ids_join_nhl() -> None:
    ids = {r.game_id for r in parse_team_games(team_text())}
    assert {2025020697, 2025020709} <= ids
    assert all(len(str(game_id)) == 10 for game_id in ids)


def test_parse_team_games_both_teams_of_a_game() -> None:
    rows = [r for r in parse_team_games(team_text()) if r.game_id == 2025020709]
    assert {r.team for r in rows} == {"EDM", "LAK"}
    edm = next(r for r in rows if r.team == "EDM")
    lak = next(r for r in rows if r.team == "LAK")
    assert (edm.xg_for, edm.xg_against) == (lak.xg_against, lak.xg_for)


def test_parse_team_games_rejects_missing_column() -> None:
    text = team_text().replace("xGoalsFor,", "xGoalsForX,", 1)
    with pytest.raises(SourceParseError, match="xGoalsFor"):
        parse_team_games(text)


BOS_GAME = ("2025020697", "BOS")


@pytest.mark.parametrize(
    "bad", ["abc", "nan", "inf", "-1", "", "1_0", " 3 ", "\u0663", "+1", "1e", "."]
)
def test_parse_team_games_rejects_bad_number(bad: str) -> None:
    text = edit_row(team_text(), *BOS_GAME, xGoalsFor=bad)
    with pytest.raises(SourceParseError, match=r"line \d+ column 'xGoalsFor'"):
        parse_team_games(text)


def test_parse_team_games_rejects_unknown_team_code() -> None:
    text = edit_row(team_text(), *BOS_GAME, team="XXX")
    with pytest.raises(SourceShapeError, match="XXX"):
        parse_team_games(text)


def test_parse_team_games_rejects_game_id_not_matching_season() -> None:
    text = edit_row(team_text(), *BOS_GAME, gameId="2024020697")
    with pytest.raises(SourceParseError, match="gameId"):
        parse_team_games(text)


@pytest.mark.parametrize("bad", ["20261340", "2026-01-10", "x"])
def test_parse_team_games_rejects_bad_date(bad: str) -> None:
    text = edit_row(team_text(), *BOS_GAME, gameDate=bad)
    with pytest.raises(SourceParseError, match="gameDate"):
        parse_team_games(text)


def test_parse_team_games_rejects_repeated_game_and_team() -> None:
    lines = team_text().splitlines()
    row = next(line for line in lines[1:] if ",all," in line and line.startswith("BOS,2025"))
    with pytest.raises(SourceParseError, match="repeats"):
        parse_team_games("\n".join([*lines, row]))


def test_parse_team_games_rejects_short_row() -> None:
    lines = team_text().splitlines()
    lines.append("BOS,2025")
    with pytest.raises(SourceParseError, match="different number of fields"):
        parse_team_games("\n".join(lines))


@pytest.mark.parametrize(
    ("code", "expected"), [("L.A", "LAK"), ("N.J", "NJD"), ("S.J", "SJS"), ("T.B", "TBL")]
)
def test_from_moneypuck_maps_dotted_codes(code: str, expected: str) -> None:
    assert from_moneypuck(code) == expected


def test_from_moneypuck_rejects_old_atlanta() -> None:
    with pytest.raises(SourceShapeError, match="ATL"):
        from_moneypuck("ATL")


def test_parse_goalie_games_reads_zip() -> None:
    rows = parse_goalie_games((FIXTURES / "goalies_2025_sample.zip").read_bytes())
    assert rows
    assert {r.game_id for r in rows} == {2025020697, 2025020709}
    ingram = next(r for r in rows if r.nhl_id == 8478971)
    assert ingram.team == "EDM"
    assert ingram.goals_against == 3
    assert ingram.xg_against > 0


def test_parse_goalie_games_rejects_zip_with_two_files() -> None:
    data = zip_of({"a.csv": "x\n", "b.csv": "y\n"})
    with pytest.raises(SourceParseError, match="one file"):
        parse_goalie_games(data)


def test_parse_goalie_games_rejects_non_zip() -> None:
    with pytest.raises(SourceParseError, match="not a ZIP"):
        parse_goalie_games(b"<html>")


def test_parse_goalie_games_rejects_missing_column() -> None:
    data = zip_of({"2025.csv": "playerId,season,gameId\n"})
    with pytest.raises(SourceParseError, match="playerTeam"):
        parse_goalie_games(data)


def test_parse_goalie_games_rejects_repeated_goalie() -> None:
    rows = zipfile.ZipFile(FIXTURES / "goalies_2025_sample.zip").read("2025.csv").decode()
    lines = rows.splitlines()
    row = next(line for line in lines[1:] if ",all," in line)
    with pytest.raises(SourceParseError, match="repeats"):
        parse_goalie_games(zip_of({"2025.csv": "\n".join([*lines, row])}))


class FakeClient(HttpClient):
    def __init__(self, bodies: dict[str, bytes]) -> None:
        super().__init__()
        self.bodies = bodies
        self.urls: list[str] = []

    def get(self, source: str, url: str) -> bytes:
        self.urls.append(url)
        return self.bodies[url]


def test_fetch_team_games_reads_the_allowed_url_only() -> None:
    client = FakeClient({TEAM_GAMES_URL: team_text().encode()})
    assert fetch_team_games(client)
    assert client.urls == [TEAM_GAMES_URL]


def test_fetch_goalie_games_builds_season_url() -> None:
    url = goalie_games_url(2025)
    client = FakeClient({url: (FIXTURES / "goalies_2025_sample.zip").read_bytes()})
    assert fetch_goalie_games(client, 2025)
    assert client.urls == [url]


def test_goalie_games_url_shape() -> None:
    assert goalie_games_url(2024) == (
        "https://peter-tanner.com/moneypuck/downloads/seasonPlayersSummary/goalies/2024.zip"
    )


def test_parse_goalie_games_refuses_oversized_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hockey_model.sources.moneypuck.MAX_CSV_BYTES", 10)
    with pytest.raises(SourceParseError, match="refusing it"):
        parse_goalie_games((FIXTURES / "goalies_2025_sample.zip").read_bytes())


def test_a_zip_with_a_forged_size_is_a_named_error() -> None:
    data = bytearray((FIXTURES / "goalies_2025_sample.zip").read_bytes())
    data[len(data) // 2] ^= 0xFF
    with pytest.raises(SourceParseError):
        parse_goalie_games(bytes(data))


def test_a_zip_member_that_is_not_utf8_is_a_named_error() -> None:
    with pytest.raises(SourceParseError, match="cannot be read"):
        parse_goalie_games(_latin1_zip())


def _latin1_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("2025.csv", b"playerId,season\n\xff\xfe\n")
    return buf.getvalue()


def test_a_team_file_that_is_not_utf8_is_a_named_error() -> None:
    client = FakeClient({TEAM_GAMES_URL: b"\xff\xfe"})
    with pytest.raises(SourceParseError, match="not UTF-8"):
        fetch_team_games(client)


def test_a_csv_field_over_the_size_limit_is_a_named_error() -> None:
    header = team_text().splitlines()[0]
    with pytest.raises(SourceParseError, match="not valid CSV"):
        parse_team_games(header + "\n" + "x" * 200_000 + "\n")


def test_a_unicode_digit_season_is_not_a_season() -> None:
    text = edit_row(team_text(), *BOS_GAME, season="٢٠٢٥")
    assert all(r.team != "BOS" or r.game_id != 2025020697 for r in parse_team_games(text))


@pytest.mark.parametrize("good", ["0", "2", "1.5", ".5", "5.", "1e-05", "3.0E2"])
def test_parse_team_games_accepts_plain_decimal_numbers(good: str) -> None:
    text = edit_row(team_text(), *BOS_GAME, xGoalsFor=good)
    rows = parse_team_games(text)
    assert any(row.xg_for == float(good) for row in rows)


def test_a_zip_header_that_understates_the_size_is_a_named_error() -> None:
    body = "playerId,season,gameId\n" + "1,2025,2025020001\n" * 20_000
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("2025.csv", body)
    true_size = len(body.encode()).to_bytes(4, "little")
    lie = (10).to_bytes(4, "little")
    patched = buf.getvalue().replace(true_size, lie)
    assert patched != buf.getvalue()
    with pytest.raises(SourceParseError, match="cannot be read"):
        parse_goalie_games(patched)


def test_a_csv_header_field_over_the_size_limit_is_a_named_error() -> None:
    with pytest.raises(SourceParseError, match="not valid CSV"):
        parse_team_games("x" * 200_000 + ",gameId\n")


def test_a_file_without_the_unblocked_columns_parses_to_the_same_rows() -> None:
    """The unblocked attempt counts are not stored, so the file need not hold them (#92)."""
    reader = csv.DictReader(io.StringIO(team_text()))
    dropped = {"unblockedShotAttemptsFor", "unblockedShotAttemptsAgainst"}
    kept = [name for name in reader.fieldnames or () if name not in dropped]
    assert len(kept) == len(reader.fieldnames or ()) - 2
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=kept, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(reader)
    assert parse_team_games(out.getvalue()) == parse_team_games(team_text())
