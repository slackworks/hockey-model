"""Tests for refresh. A fake client serves the fixtures by URL, so no test uses the network."""

import copy
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from refresh_support import (
    BOX_URL,
    ESPN,
    MAIN_GAME,
    NOW,
    SCORE_TEAMS,
    SCORE_URL,
    FakeClient,
    failing_days,
    fixture,
    minute_clock,
    score_requests,
)
from seeding import add_game, utc

from hockey_model.sources.errors import SourceError
from hockey_model.sources.moneypuck import MONEYPUCK_CREDIT, TEAM_GAMES_URL, goalie_games_url
from hockey_model.sources.nhl import score_day_url
from hockey_model.store.db import connect
from hockey_model.store.nhl_writes import games_missing_boxscore
from hockey_model.store.refresh import refresh, refresh_starters
from hockey_model.store.refresh_nhl import MAX_UNRESOLVED_DAYS, UNRESOLVED_RETRY_AFTER
from hockey_model.store.source_reads import record_read
from hockey_model.types import HockeyModelError


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def test_refresh_writes_every_source(conn: sqlite3.Connection) -> None:
    report = refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert report.errors == ()
    assert report.written["nhl_scores"] == 14
    assert report.written["nhl_boxscores"] == 14
    assert report.written["moneypuck"] > 0
    assert report.written["espn_starters"] == 2
    assert report.written["espn_injuries"] == 20
    assert count(conn, "games") == 14
    assert count(conn, "goalie_games") == 56
    assert count(conn, "starter_reads") == 2


def test_refresh_starters_reads_only_the_schedule_goalies_and_injuries(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    boxscore_rows = count(conn, "goalie_games")
    moneypuck_rows = count(conn, "mp_team_games")
    report = refresh_starters(conn, FakeClient(), now=NOW)
    assert report.errors == ()
    assert "nhl_boxscores" not in report.written
    assert "moneypuck" not in report.written
    assert "espn_injuries" in report.written
    assert count(conn, "goalie_games") == boxscore_rows
    assert count(conn, "mp_team_games") == moneypuck_rows


def test_refresh_starters_needs_a_timezone(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="has no timezone"):
        refresh_starters(conn, FakeClient(), now=datetime(2026, 1, 11, 12, 0))


def test_refresh_reports_unmatched_goalie_reads(conn: sqlite3.Connection) -> None:
    report = refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert any(
        "daily_faceoff" in note and "matched no stored game" in note for note in report.notes
    )


def test_refresh_credits_moneypuck_when_rows_written(conn: sqlite3.Connection) -> None:
    report = refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert report.credits == (MONEYPUCK_CREDIT,)


def test_refresh_isolates_source_failure(conn: sqlite3.Connection) -> None:
    tomorrow = f"{ESPN}/scoreboard?dates=20260111"
    client = FakeClient({tomorrow: b"<html>maintenance</html>"})
    report = refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 10))
    assert len(report.errors) == 1
    assert "espn" in str(report.errors[0])
    assert report.written.get("espn_starters", 0) == 0
    assert count(conn, "starter_reads") == 0
    assert report.written["nhl_scores"] == 14
    assert report.written["espn_injuries"] == 20
    failed = conn.execute(
        "SELECT source, ok FROM source_reads WHERE ok = 0 AND source = 'espn_starters'"
    ).fetchall()
    assert failed == [("espn_starters", 0)]


def test_refresh_skips_fresh_moneypuck(conn: sqlite3.Connection) -> None:
    three_days_ago = NOW - timedelta(days=3)
    record_read(conn, "moneypuck", TEAM_GAMES_URL, three_days_ago)
    for season in range(2014, 2026):
        record_read(conn, "moneypuck", goalie_games_url(season), three_days_ago)
    conn.commit()
    client = FakeClient()
    refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 10))
    assert not [url for url in client.urls if "moneypuck" in url or "peter-tanner" in url]


def test_refresh_reads_stale_moneypuck_files_again(conn: sqlite3.Connection) -> None:
    eight_days_ago = NOW - timedelta(days=8)
    record_read(conn, "moneypuck", TEAM_GAMES_URL, eight_days_ago)
    for season in range(2014, 2026):
        record_read(conn, "moneypuck", goalie_games_url(season), eight_days_ago)
    conn.commit()
    client = FakeClient()
    refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 10))
    requested = [url for url in client.urls if "moneypuck" in url or "peter-tanner" in url]
    assert sorted(requested) == sorted([TEAM_GAMES_URL, goalie_games_url(2025)])


def test_refresh_is_incremental(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    refresh(conn, FakeClient(), now=NOW + timedelta(days=2))
    client = FakeClient()
    report = refresh(conn, client, now=NOW + timedelta(days=2, hours=1))
    score_days = sorted(u[len(SCORE_URL) :] for u in client.urls if u.startswith(SCORE_URL))
    assert score_days
    assert score_days[0] >= "2026-01-11"
    assert not [u for u in client.urls if u.startswith(BOX_URL)]
    assert report.written.get("nhl_boxscores", 0) == 0
    assert not [u for u in client.urls if "peter-tanner" in u or "moneypuck" in u]


def test_refresh_reads_a_day_again_until_it_is_over(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    client = FakeClient()
    refresh(conn, client, now=NOW + timedelta(hours=1))
    assert f"{SCORE_URL}2026-01-10" in client.urls


def test_refresh_finishes_a_game_that_was_in_progress(conn: sqlite3.Connection) -> None:
    live = json.loads(fixture("nhl/score_2026-01-10.json"))
    for game in live["games"]:
        game["gameState"] = "LIVE"
    url = f"{SCORE_URL}2026-01-10"
    first = refresh(
        conn, FakeClient({url: json.dumps(live).encode()}), now=NOW, backfill_from=date(2026, 1, 10)
    )
    assert first.written.get("nhl_boxscores", 0) == 0
    later = NOW + timedelta(hours=20)
    refresh(conn, FakeClient(), now=later)
    finals = conn.execute("SELECT COUNT(*) FROM games WHERE state = 'OFF'").fetchone()[0]
    assert finals == 14
    assert count(conn, "goalie_games") == 56


def test_refresh_stops_a_source_after_three_failures_in_a_row(conn: sqlite3.Connection) -> None:
    overrides: dict[str, bytes | Exception] = {
        f"{SCORE_URL}2026-01-{day:02d}": SourceError("nhl", "u", "HTTP 503") for day in range(1, 12)
    }
    client = FakeClient(overrides)
    report = refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 1))
    assert len(report.errors) >= 3
    score_requests = [u for u in client.urls if u.startswith(SCORE_URL)]
    assert len(score_requests) == 3


def test_refresh_retries_a_failed_boxscore_only_after_a_day(conn: sqlite3.Connection) -> None:
    bad = f"{BOX_URL}2025020697/boxscore"
    refresh(
        conn,
        FakeClient({bad: SourceError("nhl", bad, "HTTP 404")}),
        now=NOW,
        backfill_from=date(2026, 1, 10),
    )
    client = FakeClient({bad: SourceError("nhl", bad, "HTTP 404")})
    refresh(conn, client, now=NOW + timedelta(hours=2))
    assert bad not in client.urls
    client = FakeClient()
    refresh(conn, client, now=NOW + timedelta(days=2))
    assert bad in client.urls


def test_a_boxscore_with_no_goalies_is_read_a_bounded_number_of_times(
    conn: sqlite3.Connection,
) -> None:
    game_id = 2025020697
    bad = f"{BOX_URL}{game_id}/boxscore"
    payload = json.loads(FakeClient._boxscore(game_id))
    for side in payload["playerByGameStats"].values():
        side["goalies"] = []
    body = json.dumps(payload).encode()
    reads = 0
    for hours in (0, 2, 48, 96, 144):
        client = FakeClient({bad: body})
        refresh(conn, client, now=NOW + timedelta(hours=hours), backfill_from=date(2026, 1, 10))
        reads += client.urls.count(bad)
    assert reads <= 2, f"the boxscore with no goalies was requested {reads} times"


def test_refresh_rejects_naive_now(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no timezone"):
        refresh(conn, FakeClient(), now=datetime(2026, 1, 10, 10, 0))


def test_refresh_uses_clock_for_each_read(conn: sqlite3.Connection) -> None:
    ticks = iter(NOW + timedelta(seconds=n) for n in range(10_000))
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10), clock=lambda: next(ticks))
    stamps = {row[0] for row in conn.execute("SELECT fetched_at FROM goalie_games")}
    assert len(stamps) > 1


def test_a_finished_season_is_read_again_when_the_last_read_was_mid_season(
    conn: sqlite3.Connection,
) -> None:
    mid_season = datetime(2026, 4, 10, tzinfo=UTC)
    record_read(conn, "moneypuck", TEAM_GAMES_URL, mid_season)
    for season in range(2014, 2026):
        record_read(conn, "moneypuck", goalie_games_url(season), mid_season)
    conn.commit()
    client = FakeClient()
    refresh(conn, client, now=datetime(2026, 12, 1, tzinfo=UTC), backfill_from=date(2026, 12, 1))
    requested = [url for url in client.urls if "peter-tanner" in url]
    assert goalie_games_url(2025) in requested
    assert goalie_games_url(2026) in requested
    assert goalie_games_url(2024) not in requested


def test_a_season_read_after_it_ended_is_not_read_again(conn: sqlite3.Connection) -> None:
    after_season = datetime(2026, 9, 2, tzinfo=UTC)
    record_read(conn, "moneypuck", TEAM_GAMES_URL, after_season)
    for season in range(2014, 2027):
        record_read(conn, "moneypuck", goalie_games_url(season), after_season)
    conn.commit()
    client = FakeClient()
    refresh(conn, client, now=datetime(2026, 12, 1, tzinfo=UTC), backfill_from=date(2026, 12, 1))
    requested = [url for url in client.urls if "peter-tanner" in url]
    assert requested == [goalie_games_url(2026)]


def test_an_evening_refresh_reads_the_eastern_game_day(conn: sqlite3.Connection) -> None:
    evening = datetime(2026, 1, 11, 1, 0, tzinfo=UTC)  # 8 pm Eastern on January 10
    client = FakeClient()
    refresh(conn, client, now=evening, backfill_from=date(2026, 1, 10))
    espn_days = sorted(u[-8:] for u in client.urls if "scoreboard?dates=" in u)
    assert espn_days == ["20260110", "20260111"]
    assert f"{SCORE_URL}2026-01-12" in client.urls
    assert f"{SCORE_URL}2026-01-13" not in client.urls


def test_a_finished_backfill_says_it_is_complete(conn: sqlite3.Connection) -> None:
    report = refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert report.backfill_complete is True
    assert refresh(conn, FakeClient(), now=NOW).backfill_complete is None


def test_a_backfill_that_spends_its_budget_stops_and_the_next_run_continues(
    conn: sqlite3.Connection,
) -> None:
    start = date(2024, 6, 1)
    first = FakeClient()
    report = refresh(
        conn,
        first,
        now=NOW,
        backfill_from=start,
        clock=minute_clock(),
        budget=timedelta(minutes=5),
    )
    assert report.backfill_complete is False
    assert report.errors == ()
    assert any("time ran out" in n for n in report.notes)
    assert any("backfill: stopped in the window" in n for n in report.notes)
    assert "2024-06-01" not in score_requests(first)

    second = FakeClient()
    done = refresh(conn, second, now=NOW + timedelta(hours=1), backfill_from=start)
    assert done.backfill_complete is True
    assert "2024-06-01" in score_requests(second)


def test_a_budget_stop_with_no_new_unit_is_an_error_that_names_the_budget(
    conn: sqlite3.Connection,
) -> None:
    start = date(2026, 1, 6)
    refresh(conn, FakeClient(), now=NOW, backfill_from=start)
    report = refresh(
        conn,
        FakeClient(),
        now=NOW,
        backfill_from=start,
        clock=minute_clock(),
        budget=timedelta(minutes=1),
    )
    assert report.backfill_complete is False
    assert any("--budget-seconds" in str(e) for e in report.errors)


def test_a_day_that_closed_since_the_last_run_does_not_count_as_progress(
    conn: sqlite3.Connection,
) -> None:
    # A daily run completes the day that closed since the last run, and then the budget stops
    # it at an open day. That day is an arrival, not a step through the backlog.
    start = date(2026, 1, 6)
    refresh(conn, FakeClient(), now=NOW, backfill_from=start)
    next_day = NOW + timedelta(days=1)
    second = FakeClient()
    report = refresh(
        conn,
        second,
        now=next_day,
        backfill_from=start,
        clock=minute_clock(next_day),
        budget=timedelta(minutes=1),
    )
    assert "2026-01-09" in score_requests(second)
    assert report.backfill_complete is False
    assert any("--budget-seconds" in str(e) for e in report.errors)


def test_a_recently_failed_boxscore_does_not_stop_the_older_windows(
    conn: sqlite3.Connection,
) -> None:
    start = date(2024, 6, 1)
    bad = f"{BOX_URL}2025020697/boxscore"
    failing: dict[str, bytes | Exception] = {bad: SourceError("nhl", bad, "HTTP 404")}
    refresh(conn, FakeClient(failing), now=NOW, backfill_from=date(2026, 1, 10))
    client = FakeClient(failing)
    report = refresh(conn, client, now=NOW + timedelta(hours=2), backfill_from=start)
    assert report.backfill_complete is False
    assert "2024-06-01" in score_requests(client)
    assert any("some days or games are unread" in n for n in report.notes)


def test_a_backfill_from_a_future_day_is_an_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(HockeyModelError, match="is after"):
        refresh(conn, FakeClient(), now=NOW, backfill_from=date(2030, 1, 1))


def test_a_budget_needs_a_clock(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="needs a clock"):
        refresh(conn, FakeClient(), now=NOW, budget=timedelta(minutes=1))


def test_a_backfill_with_a_failed_day_is_not_complete(conn: sqlite3.Connection) -> None:
    report = refresh(
        conn,
        FakeClient(failing_days("2026-01-05")),
        now=NOW,
        backfill_from=date(2026, 1, 1),
    )
    assert report.backfill_complete is False
    assert len(report.errors) == 1


def test_a_stopped_source_says_how_much_it_left(conn: sqlite3.Connection) -> None:
    overrides: dict[str, bytes | Exception] = {
        f"{SCORE_URL}2026-01-{day:02d}": SourceError("nhl", "u", "HTTP 503") for day in range(1, 12)
    }
    report = refresh(conn, FakeClient(overrides), now=NOW, backfill_from=date(2026, 1, 1))
    assert any("nhl_scores: stopped after 3 failed days; 9 not tried" in n for n in report.notes)


def test_skipped_boxscores_are_counted_in_the_notes(conn: sqlite3.Connection) -> None:
    bad = f"{BOX_URL}2025020697/boxscore"
    refresh(
        conn,
        FakeClient({bad: SourceError("nhl", bad, "HTTP 404")}),
        now=NOW,
        backfill_from=date(2026, 1, 10),
    )
    report = refresh(
        conn, FakeClient({bad: SourceError("nhl", bad, "HTTP 404")}), now=NOW + timedelta(hours=2)
    )
    assert any("nhl_boxscores: 1 skipped" in n for n in report.notes)


def test_a_failed_score_day_is_read_again_by_the_next_refresh(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(failing_days("2026-01-07")), now=NOW, backfill_from=date(2026, 1, 6))
    client = FakeClient()
    report = refresh(conn, client, now=NOW + timedelta(hours=1))
    assert "2026-01-07" in score_requests(client)
    assert report.errors == ()


def test_a_score_day_that_worked_is_not_read_again_for_an_old_failure(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(failing_days("2026-01-07")), now=NOW, backfill_from=date(2026, 1, 6))
    refresh(conn, FakeClient(), now=NOW + timedelta(hours=1))
    client = FakeClient()
    refresh(conn, client, now=NOW + timedelta(hours=2))
    assert "2026-01-07" not in score_requests(client)


def test_a_failed_score_day_older_than_the_window_is_left_alone(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(failing_days("2025-11-01")), now=NOW, backfill_from=date(2025, 11, 1))
    client = FakeClient()
    refresh(conn, client, now=NOW + timedelta(hours=1))
    assert "2025-11-01" not in score_requests(client)


def test_a_score_day_that_failed_in_two_reads_is_skipped_and_named(
    conn: sqlite3.Connection,
) -> None:
    failing = failing_days("2025-11-01")
    for days in (0, 1):
        report = refresh(
            conn,
            FakeClient(failing),
            now=NOW + timedelta(days=days),
            backfill_from=date(2025, 11, 1),
        )
        assert len(report.errors) == 1
    client = FakeClient(failing)
    report = refresh(conn, client, now=NOW + timedelta(days=2), backfill_from=date(2025, 11, 1))
    assert "2025-11-01" not in score_requests(client)
    assert report.errors == ()
    assert any("nhl_scores: skipped 2025-11-01, which failed" in n for n in report.notes)


def test_a_score_day_with_one_failed_read_is_still_read_in_a_backfill(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(failing_days("2025-11-01")), now=NOW, backfill_from=date(2025, 11, 1))
    client = FakeClient()
    report = refresh(conn, client, now=NOW + timedelta(hours=1), backfill_from=date(2025, 11, 1))
    assert "2025-11-01" in score_requests(client)
    assert report.errors == ()


def test_a_boxscore_that_failed_in_two_reads_does_not_stop_the_later_games(
    conn: sqlite3.Connection,
) -> None:
    bad = f"{BOX_URL}2025020697/boxscore"
    failing = FakeClient({bad: SourceError("nhl", bad, "HTTP 404")})
    refresh(conn, failing, now=NOW, backfill_from=date(2026, 1, 10))
    refresh(conn, failing, now=NOW + timedelta(days=2))
    client = FakeClient({bad: SourceError("nhl", bad, "HTTP 404")})
    report = refresh(conn, client, now=NOW + timedelta(days=4))
    assert bad not in client.urls
    assert report.errors == ()
    assert any("nhl_boxscores: 1 games skipped" in n for n in report.notes)


def test_failed_days_apart_do_not_stop_the_refresh_before_today(conn: sqlite3.Connection) -> None:
    days = ("2026-01-02", "2026-01-04", "2026-01-06")
    refresh(conn, FakeClient(failing_days(*days)), now=NOW, backfill_from=date(2026, 1, 1))
    client = FakeClient(failing_days(*days))
    refresh(conn, client, now=NOW + timedelta(hours=1))
    assert "2026-01-10" in score_requests(client)


def _fresh_moneypuck(conn: sqlite3.Connection, now: datetime, last_season: int) -> None:
    """Record fresh reads of the team file and every goalie file through ``last_season``."""
    record_read(conn, "moneypuck", TEAM_GAMES_URL, now - timedelta(days=1))
    for season in range(2014, last_season + 1):
        record_read(conn, "moneypuck", goalie_games_url(season), now - timedelta(days=1))
    conn.commit()


@pytest.mark.parametrize(
    ("moment", "new_season_requested"),
    [
        (datetime(2026, 7, 15, tzinfo=UTC), False),
        (datetime(2026, 8, 31, tzinfo=UTC), False),
        # 00:00 UTC on Sept 1 is still Aug 31 in Eastern time, the game day the NHL uses.
        (datetime(2026, 9, 1, tzinfo=UTC), False),
        (datetime(2026, 9, 1, 4, tzinfo=UTC), True),
    ],
)
def test_the_new_goalie_file_is_read_from_the_shared_season_boundary(
    conn: sqlite3.Connection, moment: datetime, new_season_requested: bool
) -> None:
    """Refresh counts the new season from the Eastern game day, as the NHL schedule does."""
    _fresh_moneypuck(conn, moment, 2025)
    client = FakeClient()
    refresh(conn, client, now=moment, backfill_from=date(2026, 1, 10))
    assert (goalie_games_url(2026) in client.urls) is new_season_requested


def _box_url(game_id: int) -> str:
    return f"{BOX_URL}{game_id}/boxscore"


def _boxscore_with_teams(game_id: int, home: str, away: str) -> bytes:
    payload = json.loads(fixture("nhl/boxscore_2025020709.json"))
    payload["id"] = game_id
    payload["homeTeam"]["abbrev"], payload["awayTeam"]["abbrev"] = home, away
    return json.dumps(payload).encode()


GAME_ID = MAIN_GAME


@pytest.mark.parametrize(
    ("swap", "label"),
    [
        (lambda home, away: (away, home), "swapped home and away"),
        (lambda home, away: (home, "VAN"), "wrong away team"),
        (lambda home, away: ("VAN", away), "wrong home team"),
    ],
)
def test_a_boxscore_for_other_teams_is_rejected_and_writes_nothing(
    conn: sqlite3.Connection, swap, label: str
) -> None:
    home, away = SCORE_TEAMS[GAME_ID]
    wrong = _boxscore_with_teams(GAME_ID, *swap(home, away))
    client = FakeClient({_box_url(GAME_ID): wrong})
    report = refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 10))
    assert any(f"{GAME_ID}" in str(error) and "names" in str(error) for error in report.errors)
    assert conn.execute(
        "SELECT COUNT(*) FROM goalie_games WHERE game_id = ?", (GAME_ID,)
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM skater_games WHERE game_id = ?", (GAME_ID,)
    ).fetchone() == (0,)
    failed = conn.execute(
        "SELECT ok FROM source_reads WHERE source = 'nhl_boxscores' AND url = ?",
        (_box_url(GAME_ID),),
    ).fetchall()
    assert (0,) in failed
    assert GAME_ID in games_missing_boxscore(conn)


def test_a_boxscore_for_the_stored_teams_is_accepted(conn: sqlite3.Connection) -> None:
    report = refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    assert report.errors == ()
    assert GAME_ID not in games_missing_boxscore(conn)


def test_a_partial_stored_game_is_repaired_and_a_failed_repair_keeps_the_old_rows(
    conn: sqlite3.Connection,
) -> None:
    refresh(conn, FakeClient(), now=NOW, backfill_from=date(2026, 1, 10))
    home, away = SCORE_TEAMS[GAME_ID]
    # An old partial record: the away side's rows are gone.
    conn.execute("DELETE FROM goalie_games WHERE game_id = ? AND team = ?", (GAME_ID, away))
    conn.execute("DELETE FROM skater_games WHERE game_id = ? AND team = ?", (GAME_ID, away))
    conn.commit()
    assert GAME_ID in games_missing_boxscore(conn)
    kept = conn.execute(
        "SELECT * FROM skater_games WHERE game_id = ? ORDER BY nhl_id", (GAME_ID,)
    ).fetchall()

    later = NOW + timedelta(days=2)
    bad = _boxscore_with_teams(GAME_ID, away, home)
    refresh(conn, FakeClient({_box_url(GAME_ID): bad}), now=later)
    assert (
        conn.execute(
            "SELECT * FROM skater_games WHERE game_id = ? ORDER BY nhl_id", (GAME_ID,)
        ).fetchall()
        == kept
    )

    refresh(conn, FakeClient(), now=later + timedelta(days=2))
    assert GAME_ID not in games_missing_boxscore(conn)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM skater_games WHERE game_id = ? AND team = ?", (GAME_ID, away)
        ).fetchone()[0]
        > 0
    )


def _score_payload(game_id: int, day: str, state: str, home: str, away: str) -> bytes:
    """A score day with one game, built from a fixture game."""
    payload = json.loads(fixture("nhl/score_2026-01-10.json"))
    game = copy.deepcopy(payload["games"][0])
    game["id"] = game_id
    game["gameState"] = state
    game["gameDate"] = day
    game["startTimeUTC"] = f"{day}T18:00:00Z"
    game["homeTeam"]["abbrev"], game["awayTeam"]["abbrev"] = home, away
    payload["games"] = [game]
    return json.dumps(payload).encode()


def _unresolved_store(conn: sqlite3.Connection) -> None:
    """A LIVE game on Jan 5 that a later read marked complete, then a newer final on Jan 9."""
    add_game(conn, 9001, "2026-01-05T18:00", state="LIVE", fetched_at=utc("2026-01-05T20:00"))
    record_read(conn, "nhl_scores", score_day_url(date(2026, 1, 5)), utc("2026-01-08T00:00"))
    add_game(conn, 9002, "2026-01-09T18:00", state="OFF", fetched_at=utc("2026-01-10T00:00"))
    conn.commit()


def _requests_for(client: FakeClient, day: date) -> int:
    return client.urls.count(score_day_url(day))


def test_an_old_unresolved_game_is_read_again_after_newer_finals(conn: sqlite3.Connection) -> None:
    _unresolved_store(conn)
    day_url = score_day_url(date(2026, 1, 5))
    # The fake serves the fixture boxscore, whose teams are EDM at LAK.
    overrides: dict[str, bytes | Exception] = {
        day_url: _score_payload(9001, "2026-01-05", "OFF", "EDM", "LAK"),
    }
    client = FakeClient(overrides)
    report = refresh(conn, client, now=NOW)
    # Game 9002 is seeded without a boxscore, and the fixture does not match its teams.
    assert not [error for error in report.errors if "9001" in str(error)]
    assert _requests_for(client, date(2026, 1, 5)) == 1
    (state,) = conn.execute("SELECT state FROM latest_games WHERE game_id = 9001").fetchone()
    assert state == "OFF"
    assert 9001 not in games_missing_boxscore(conn)


def test_a_day_with_no_unresolved_game_is_not_read_again(conn: sqlite3.Connection) -> None:
    add_game(conn, 9001, "2026-01-05T18:00", state="OFF", fetched_at=utc("2026-01-05T23:00"))
    record_read(conn, "nhl_scores", score_day_url(date(2026, 1, 5)), utc("2026-01-08T00:00"))
    add_game(conn, 9002, "2026-01-09T18:00", state="OFF", fetched_at=utc("2026-01-10T00:00"))
    conn.commit()
    client = FakeClient()
    refresh(conn, client, now=NOW)
    assert _requests_for(client, date(2026, 1, 5)) == 0


def test_an_unresolved_game_waits_out_a_cooldown_between_reads(conn: sqlite3.Connection) -> None:
    _unresolved_store(conn)
    still_live = _score_payload(9001, "2026-01-05", "LIVE", "TOR", "BOS")
    overrides: dict[str, bytes | Exception] = {score_day_url(date(2026, 1, 5)): still_live}
    first = FakeClient(overrides)
    refresh(conn, first, now=NOW)
    assert _requests_for(first, date(2026, 1, 5)) == 1

    soon = FakeClient(overrides)
    refresh(conn, soon, now=NOW + timedelta(hours=1))
    assert _requests_for(soon, date(2026, 1, 5)) == 0

    later = FakeClient(overrides)
    refresh(conn, later, now=NOW + UNRESOLVED_RETRY_AFTER + timedelta(hours=1))
    assert _requests_for(later, date(2026, 1, 5)) == 1


def test_an_unresolved_game_that_stays_empty_is_listed_not_dropped(
    conn: sqlite3.Connection,
) -> None:
    _unresolved_store(conn)
    report = refresh(conn, FakeClient(), now=NOW)
    assert any("not final" in note and "9001" in note for note in report.notes)
    (state,) = conn.execute("SELECT state FROM latest_games WHERE game_id = 9001").fetchone()
    assert state == "LIVE"


def test_a_run_reads_a_bounded_number_of_unresolved_days(conn: sqlite3.Connection) -> None:
    for offset in range(MAX_UNRESOLVED_DAYS + 5):
        day = date(2025, 12, 1) + timedelta(days=offset)
        add_game(
            conn,
            8000 + offset,
            f"{day.isoformat()}T18:00",
            state="LIVE",
            fetched_at=utc(f"{day.isoformat()}T20:00"),
        )
        record_read(
            conn,
            "nhl_scores",
            score_day_url(day),
            utc(f"{day.isoformat()}T00:00") + timedelta(days=2),
        )
    add_game(conn, 9002, "2026-01-09T18:00", state="OFF", fetched_at=utc("2026-01-10T00:00"))
    conn.commit()
    client = FakeClient()
    refresh(conn, client, now=NOW)
    old_days = [
        u for u in client.urls if u.startswith(SCORE_URL) and u[len(SCORE_URL) :] < "2026-01-08"
    ]
    assert len(old_days) == MAX_UNRESOLVED_DAYS


def test_an_explicit_backfill_reads_an_old_unresolved_day(conn: sqlite3.Connection) -> None:
    _unresolved_store(conn)
    client = FakeClient()
    refresh(conn, client, now=NOW, backfill_from=date(2026, 1, 5))
    assert _requests_for(client, date(2026, 1, 5)) == 1


def test_a_source_failure_stop_does_not_blame_the_budget(conn: sqlite3.Connection) -> None:
    # Three score days fail in a row, so the read stops on the failures. The budget is also
    # spent by then. The stop came from the source, so no error tells the operator to raise
    # the budget.
    start = date(2026, 1, 6)
    failing = failing_days("2026-01-06", "2026-01-07", "2026-01-08")
    client = FakeClient(failing)

    def spent_after_three_reads() -> datetime:
        # The clock passes the budget only once the three failing reads have happened, so the
        # failure stop comes first and the budget is spent at the end of the run.
        return NOW + timedelta(hours=2) if len(client.urls) >= 3 else NOW

    report = refresh(
        conn,
        client,
        now=NOW,
        backfill_from=start,
        clock=spent_after_three_reads,
        budget=timedelta(hours=1),
    )
    assert report.backfill_complete is False
    assert any("stopped after 3 failed days" in n for n in report.notes)
    assert not any("--budget-seconds" in str(e) for e in report.errors)
