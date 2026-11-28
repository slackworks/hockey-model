"""Tests for the reconciliation of one stored game (#35)."""

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from seeding import add_game

from hockey_model.sources.espn_summary import EspnSummary, parse_summary
from hockey_model.sources.nhl import BOXSCORE_PARSER, parse_boxscore
from hockey_model.sources.nhl_events import parse_pbp
from hockey_model.sources.nhl_shifts import parse_shifts
from hockey_model.store import reconcile_game as reconcile_module
from hockey_model.store.archive import OK, SHAPE_ERROR, Fetch, record_observation
from hockey_model.store.boxscore_revisions import record_revision
from hockey_model.store.db import StoreError, connect
from hockey_model.store.event_writes import write_espn, write_pbp, write_shifts
from hockey_model.store.reconcile_game import reconcile_game, reconcile_games
from hockey_model.store.source_reads import BOXSCORES, ESPN_SUMMARY, NHL_PBP, NHL_SHIFTS

FIXTURES = Path(__file__).parent.parent / "fixtures"
GAME = 2025020709
START = datetime(2026, 1, 10, 1, 0, tzinfo=UTC)
EARLY = START + timedelta(days=1)
LATE = START + timedelta(days=15)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    add_game(connection, GAME, START.isoformat(), home="EDM", away="LAK", score=(3, 4))
    connection.execute("UPDATE games SET last_period = 'SO'")
    yield connection
    connection.close()


def _json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def _obs(conn: sqlite3.Connection, source: str, when: datetime) -> int:
    return record_observation(
        conn, Fetch(source, "https://example.test/u", when, OK, f"{source}:1", str(GAME), b"{}")
    )


def _store(conn: sqlite3.Connection, when: datetime, *, events=None) -> tuple[int, int]:
    pbp, shift = _obs(conn, NHL_PBP, when), _obs(conn, NHL_SHIFTS, when)
    rows = parse_pbp(_json("nhl/pbp_2025020709.json"), GAME)
    write_pbp(conn, pbp, rows if events is None else replace(rows, events=events))
    write_shifts(conn, shift, parse_shifts(_json("nhl/shifts_2025020709.json"), GAME))
    return pbp, shift


def _boxscore(conn: sqlite3.Connection, when: datetime = LATE) -> None:
    body = (FIXTURES / "nhl" / "boxscore_2025020709.json").read_bytes()
    fetch = Fetch(BOXSCORES, "https://example.test/b", when, OK, BOXSCORE_PARSER, str(GAME), body)
    obs = record_observation(conn, fetch)
    record_revision(conn, GAME, parse_boxscore(json.loads(body)), obs)


def _status(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT * FROM event_status ORDER BY pbp_obs, shift_obs").fetchall()


def _checks(conn: sqlite3.Connection) -> dict[str, bool]:
    notes = json.loads(conn.execute("SELECT notes FROM latest_event_status").fetchone()[0])
    return {n["check"]: n["passed"] for n in notes}


def test_a_complete_game_after_the_last_revisit_is_reconciled(conn: sqlite3.Connection) -> None:
    _boxscore(conn)
    pbp, shift = _store(conn, LATE)
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "reconciled"
    row = _status(conn)[0]
    assert row[:4] == (GAME, pbp, shift, "reconciled")
    assert row[4] == "2026-01-25T01:00:00.000000+00:00"
    assert _checks(conn) == {
        "goals_by_team": True,
        "goals_by_player": True,
        "roster": True,
        "situation": True,
        "game_state": True,
        "espn_teams": True,
        "shift_lengths": True,
    }


def test_a_game_read_before_the_last_revisit_is_provisional(conn: sqlite3.Connection) -> None:
    _boxscore(conn)
    _store(conn, EARLY)
    result = reconcile_game(conn, GAME, EARLY)
    assert result is not None
    assert result.status == "provisional"
    assert all(_checks(conn).values())


def test_a_missing_goal_makes_a_settled_game_incomplete_and_names_the_checks(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn)
    events = parse_pbp(_json("nhl/pbp_2025020709.json"), GAME).events
    first_goal = next(i for i, e in enumerate(events) if e.type == "goal")
    _store(conn, LATE, events=events[:first_goal] + events[first_goal + 1 :])
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "incomplete"
    checks = _checks(conn)
    assert checks["goals_by_team"] is False
    assert checks["goals_by_player"] is False


def test_a_missing_goal_leaves_the_game_provisional_before_the_last_revisit(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn)
    events = parse_pbp(_json("nhl/pbp_2025020709.json"), GAME).events
    first_goal = next(i for i, e in enumerate(events) if e.type == "goal")
    _store(conn, EARLY, events=events[:first_goal] + events[first_goal + 1 :])
    result = reconcile_game(conn, GAME, EARLY)
    assert result is not None
    assert result.status == "provisional"
    assert _checks(conn)["goals_by_team"] is False


def test_a_game_with_no_boxscore_fails_the_player_check_with_a_reason(
    conn: sqlite3.Connection,
) -> None:
    _store(conn, LATE)
    reconcile_game(conn, GAME, LATE)
    notes = json.loads(conn.execute("SELECT notes FROM event_status").fetchone()[0])
    entry = next(n for n in notes if n["check"] == "goals_by_player")
    assert entry == {
        "check": "goals_by_player",
        "passed": False,
        "detail": {"reason": "the game has no boxscore"},
    }


def test_a_game_with_no_final_score_fails_the_team_check(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE games SET state = 'LIVE', home_score = NULL, away_score = NULL")
    _boxscore(conn)
    _store(conn, LATE)
    reconcile_game(conn, GAME, LATE)
    assert _checks(conn)["goals_by_team"] is False


def _espn(conn: sqlite3.Connection, summary: EspnSummary) -> None:
    obs = record_observation(
        conn, Fetch(ESPN_SUMMARY, "https://example.test/e", LATE, OK, "espn_summary:1", str(GAME))
    )
    write_espn(conn, obs, GAME, summary)


def test_an_espn_summary_of_the_two_teams_passes_the_team_check(conn: sqlite3.Connection) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    _espn(conn, parse_summary(_json("espn/summary_401803060.json"), "401803060"))
    reconcile_game(conn, GAME, LATE)
    assert _checks(conn)["espn_teams"] is True


def test_an_espn_summary_of_another_team_fails_the_team_check(conn: sqlite3.Connection) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    summary = parse_summary(_json("espn/summary_401803060.json"), "401803060")
    skaters = tuple(replace(s, team="CGY") if s.team == "LAK" else s for s in summary.skaters)
    _espn(conn, EspnSummary(skaters))
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "incomplete"
    assert _checks(conn)["espn_teams"] is False


def test_a_game_with_no_espn_summary_notes_that_the_team_check_was_skipped(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    reconcile_game(conn, GAME, LATE)
    notes = json.loads(conn.execute("SELECT notes FROM event_status").fetchone()[0])
    entry = next(n for n in notes if n["check"] == "espn_teams")
    assert entry == {
        "check": "espn_teams",
        "passed": True,
        "detail": {"skipped": "no ESPN summary is stored for the game"},
    }


def test_a_refused_newest_espn_summary_fails_the_team_check(conn: sqlite3.Connection) -> None:
    """An older summary of the wrong team must not be forgotten when a newer one is refused."""
    _boxscore(conn)
    _store(conn, LATE)
    summary = parse_summary(_json("espn/summary_401803060.json"), "401803060")
    wrong = EspnSummary(
        tuple(replace(s, team="CGY") if s.team == "LAK" else s for s in summary.skaters)
    )
    _espn(conn, wrong)
    when = LATE + timedelta(hours=1)
    refused = Fetch(ESPN_SUMMARY, "u", when, SHAPE_ERROR, "espn_summary:1", str(GAME), b"{}")
    record_observation(conn, refused)
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "incomplete"
    notes = json.loads(conn.execute("SELECT notes FROM event_status").fetchone()[0])
    entry = next(n for n in notes if n["check"] == "espn_teams")
    assert entry["passed"] is False
    assert entry["detail"] == {"reason": "the newest ESPN summary has no rows"}


def test_a_second_run_replaces_the_row_of_the_same_pair(conn: sqlite3.Connection) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    reconcile_game(conn, GAME, LATE)
    reconcile_game(conn, GAME, LATE + timedelta(hours=1))
    rows = _status(conn)
    assert len(rows) == 1
    assert rows[0][4] == "2026-01-25T02:00:00.000000+00:00"


def test_a_game_with_no_shift_rows_gives_no_status(conn: sqlite3.Connection) -> None:
    pbp = _obs(conn, NHL_PBP, LATE)
    write_pbp(conn, pbp, parse_pbp(_json("nhl/pbp_2025020709.json"), GAME))
    assert reconcile_game(conn, GAME, LATE) is None
    assert _status(conn) == []


def test_a_check_that_raises_leaves_the_stints_and_the_status_as_they_were(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boxscore(conn)
    _store(conn, LATE)

    def boom(*args: object) -> list[object]:
        raise StoreError("game 1: a check failed")

    monkeypatch.setattr(reconcile_module, "_checks", boom)
    with pytest.raises(StoreError, match="a check failed"):
        reconcile_game(conn, GAME, LATE)
    assert conn.execute("SELECT COUNT(*) FROM stints").fetchone() == (0,)
    assert _status(conn) == []


def test_a_game_with_one_early_and_one_late_read_is_provisional(conn: sqlite3.Connection) -> None:
    """The newer of the two reads does not settle a game. The older one decides."""
    _boxscore(conn)
    pbp, shift = _obs(conn, NHL_PBP, LATE), _obs(conn, NHL_SHIFTS, EARLY)
    write_pbp(conn, pbp, parse_pbp(_json("nhl/pbp_2025020709.json"), GAME))
    write_shifts(conn, shift, parse_shifts(_json("nhl/shifts_2025020709.json"), GAME))
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "provisional"


@pytest.mark.parametrize("column", ["start_utc", "fetched_at"])
def test_a_stored_time_that_does_not_parse_is_a_store_error_that_names_the_game(
    conn: sqlite3.Connection, column: str
) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    if column == "start_utc":
        conn.execute("UPDATE games SET start_utc = 'garbage'")
    else:
        conn.execute("UPDATE observations SET fetched_at = 'garbage' WHERE source = ?", (NHL_PBP,))
    with pytest.raises(StoreError, match=rf"game {GAME}: .*garbage"):
        reconcile_game(conn, GAME, LATE)


@pytest.mark.parametrize(
    ("column", "value", "reason"),
    [
        ("completeness", "incomplete", "the newest boxscore revision is incomplete"),
        ("identity", "mismatched", "the newest boxscore revision names another game or teams"),
    ],
)
def test_a_boxscore_revision_that_is_not_sound_fails_the_player_check_with_a_reason(
    conn: sqlite3.Connection, column: str, value: str, reason: str
) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    conn.execute(f"UPDATE boxscore_revisions SET {column} = ?", (value,))
    reconcile_game(conn, GAME, LATE)
    notes = json.loads(conn.execute("SELECT notes FROM event_status").fetchone()[0])
    entry = next(n for n in notes if n["check"] == "goals_by_player")
    assert entry["passed"] is False
    assert entry["detail"] == {"reason": reason}


def test_an_unverified_boxscore_revision_is_read_and_the_check_can_pass(
    conn: sqlite3.Connection,
) -> None:
    """A revision from before the identity check is read, and the player goals still decide."""
    _boxscore(conn)
    _store(conn, LATE)
    conn.execute("UPDATE boxscore_revisions SET identity = 'unverified'")
    reconcile_game(conn, GAME, LATE)
    assert _checks(conn)["goals_by_player"] is True


def test_a_shift_that_ends_before_it_starts_makes_a_settled_game_incomplete(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn)
    pbp, shift = _obs(conn, NHL_PBP, LATE), _obs(conn, NHL_SHIFTS, LATE)
    write_pbp(conn, pbp, parse_pbp(_json("nhl/pbp_2025020709.json"), GAME))
    rows = parse_shifts(_json("nhl/shifts_2025020709.json"), GAME)
    inverted = replace(rows.shifts[0], start_second=500, end_second=400)
    write_shifts(conn, shift, replace(rows, shifts=(*rows.shifts, inverted)))
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "incomplete"
    assert _checks(conn)["shift_lengths"] is False


def test_a_goal_with_no_owner_team_fails_the_team_check(conn: sqlite3.Connection) -> None:
    _boxscore(conn)
    events = parse_pbp(_json("nhl/pbp_2025020709.json"), GAME).events
    first = next(i for i, e in enumerate(events) if e.type == "goal")
    nameless = replace(events[first], owner_team=None)
    _store(conn, LATE, events=(*events[:first], nameless, *events[first + 1 :]))
    reconcile_game(conn, GAME, LATE)
    assert _checks(conn)["goals_by_team"] is False
    notes = json.loads(conn.execute("SELECT notes FROM event_status").fetchone()[0])
    entry = next(n for n in notes if n["check"] == "goals_by_team")
    assert entry["detail"]["found"]["null"] == 1


def test_a_play_by_play_that_is_still_live_fails_the_game_state_check(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn)
    pbp, _ = _store(conn, LATE)
    conn.execute("UPDATE pbp_events SET game_state = 'LIVE' WHERE obs_id = ?", (pbp,))
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "incomplete"
    assert _checks(conn)["game_state"] is False


def _served(conn: sqlite3.Connection) -> tuple[int, int]:
    stints = conn.execute("SELECT COUNT(*) FROM latest_stints").fetchone()[0]
    return stints, conn.execute("SELECT COUNT(*) FROM latest_event_status").fetchone()[0]


def test_a_rerun_that_fails_removes_the_old_status_and_stints_of_the_game(
    conn: sqlite3.Connection,
) -> None:
    """The probe of the review: the first run is reconciled, and the inputs stop parsing."""
    _boxscore(conn)
    _store(conn, LATE)
    first = reconcile_game(conn, GAME, LATE)
    assert first is not None
    assert first.status == "reconciled"
    assert _served(conn)[1] == 1
    conn.execute("UPDATE games SET start_utc = 'garbage'")
    with pytest.raises(StoreError, match="garbage"):
        reconcile_game(conn, GAME, LATE)
    assert _served(conn) == (0, 0)
    assert _status(conn) == []


def test_a_rerun_that_fails_in_the_builder_removes_the_old_status_too(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    reconcile_game(conn, GAME, LATE)
    conn.execute("UPDATE shifts SET team = 'CGY' WHERE rowid = (SELECT MIN(rowid) FROM shifts)")
    with pytest.raises(StoreError, match="neither the home team"):
        reconcile_game(conn, GAME, LATE)
    assert _served(conn) == (0, 0)


def test_a_failed_game_loses_its_status_and_another_game_keeps_its_own(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    reconcile_game(conn, GAME, LATE)
    conn.execute("UPDATE games SET start_utc = 'garbage'")
    report = reconcile_games(conn, LATE)
    assert report.failed
    assert _served(conn) == (0, 0)


def test_a_shift_chart_with_no_goalie_shift_cannot_reach_reconciled(
    conn: sqlite3.Connection,
) -> None:
    """The probe of the review: every goalie shift is deleted, and the nets read as empty."""
    _boxscore(conn)
    _store(conn, LATE)
    goalies = "SELECT player_id FROM pbp_roster WHERE position = 'G'"
    conn.execute(f"DELETE FROM shifts WHERE player_id IN ({goalies})")
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "incomplete"
    checks = _checks(conn)
    assert checks["situation"] is False
    assert checks["roster"] is True
    assert checks["shift_lengths"] is True


def test_a_boxscore_read_before_the_last_revisit_keeps_the_game_provisional(
    conn: sqlite3.Connection,
) -> None:
    """The play-by-play and the shifts are late, and the boxscore is the early one."""
    _boxscore(conn, EARLY)
    _store(conn, LATE)
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "provisional"


def test_a_boxscore_read_after_the_last_revisit_settles_the_game(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn, LATE + timedelta(days=1))
    _store(conn, LATE)
    result = reconcile_game(conn, GAME, LATE)
    assert result is not None
    assert result.status == "reconciled"


def test_a_run_for_a_newer_pair_removes_the_rows_of_the_older_pair(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    reconcile_game(conn, GAME, LATE)
    newest = _store(conn, LATE + timedelta(days=1))
    reconcile_game(conn, GAME, LATE + timedelta(days=1))
    pairs = {(row[0], row[1]) for row in conn.execute("SELECT pbp_obs, shift_obs FROM stints")}
    assert pairs == {newest}
    assert [(row[1], row[2]) for row in _status(conn)] == [newest]


def test_a_run_for_one_game_leaves_the_rows_of_another_game(conn: sqlite3.Connection) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    reconcile_game(conn, GAME, LATE)
    conn.execute(
        "INSERT INTO event_status (game_id, pbp_obs, shift_obs, status, checked_at, notes)"
        " VALUES (?, 1, 2, 'reconciled', 'x', '[]')",
        (GAME + 1,),
    )
    reconcile_game(conn, GAME, LATE)
    assert conn.execute(
        "SELECT COUNT(*) FROM event_status WHERE game_id = ?", (GAME + 1,)
    ).fetchone() == (1,)


def test_a_boxscore_revision_with_a_bad_time_fails_only_its_game(
    conn: sqlite3.Connection,
) -> None:
    _boxscore(conn)
    _store(conn, LATE)
    assert reconcile_game(conn, GAME, LATE) is not None
    conn.execute("UPDATE boxscore_revisions SET fetched_at = 'garbage'")
    report = reconcile_games(conn, LATE, [GAME])
    assert len(report.failed) == 1
    assert f"game {GAME}" in report.failed[0]
    assert "garbage" in report.failed[0]
    assert _status(conn) == []
