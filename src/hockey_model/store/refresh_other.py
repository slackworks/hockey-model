"""The MoneyPuck, ESPN, and Daily Faceoff steps of a refresh."""

from datetime import UTC, date, datetime, timedelta

from hockey_model.game_day import game_day_of
from hockey_model.game_model.seasons import SEASON_START_MONTH, season_start_year
from hockey_model.sources.daily_faceoff import STARTERS_URL, fetch_starters
from hockey_model.sources.espn import ESPN_BASE, fetch_injuries, fetch_scoreboard_starters
from hockey_model.sources.moneypuck import (
    FIRST_SEASON,
    MONEYPUCK_CREDIT,
    REFRESH_AFTER,
    TEAM_GAMES_URL,
    fetch_goalie_games,
    fetch_team_games,
    goalie_games_url,
)
from hockey_model.sources.starters import StarterRead
from hockey_model.store.espn_writes import write_injury_reads
from hockey_model.store.moneypuck_writes import (
    write_goalie_games,
    write_team_games,
)
from hockey_model.store.refresh_context import MAX_UNIT_FAILURES, RefreshRun
from hockey_model.store.source_reads import ESPN_INJURIES, last_ok_read
from hockey_model.store.starter_writes import write_starter_reads

MONEYPUCK = "moneypuck"
ESPN_STARTERS = "espn_starters"
DAILY_FACEOFF = "daily_faceoff"
_UNMATCHED_SHOWN = 5


def _stale(run: RefreshRun, url: str) -> bool:
    if run.reread_moneypuck:
        return True
    last = last_ok_read(run.conn, MONEYPUCK, url)
    return last is None or run.now - last >= REFRESH_AFTER


def _goalie_file_stale(run: RefreshRun, season: int, current: int) -> bool:
    """Return True when the goalie file of ``season`` needs a read.

    The file of the current season is read again after a week. The file of a finished
    season is read once, and that read must come after the season ended, so a read from
    the middle of the season does not leave the last weeks and the playoffs missing.
    """
    url = goalie_games_url(season)
    if season == current:
        return _stale(run, url)
    last = last_ok_read(run.conn, MONEYPUCK, url)
    season_over = datetime(season + 1, SEASON_START_MONTH, 1, tzinfo=UTC)
    return last is None or last < season_over


def _note_credit(run: RefreshRun, rows_written: int) -> int:
    if rows_written and MONEYPUCK_CREDIT not in run.credits:
        run.credits.append(MONEYPUCK_CREDIT)
    return rows_written


def _stop_for_time(run: RefreshRun, unit: str, tried: int) -> bool:
    """Return True, with a note, once the time budget is spent and a file was tried.

    A download that has begun cannot stop, so the check comes before each file. The first file
    of a run is always tried, so a run that the NHL steps left no time still makes progress.
    A backfill that reached this step is not complete while a file is waiting.
    """
    if tried == 0 or not run.out_of_time():
        return False
    run.notes.append(f"{MONEYPUCK}: stopped before {unit}, because the time ran out")
    _mark_incomplete(run)
    return True


def _mark_incomplete(run: RefreshRun) -> None:
    """Say that a backfill stopped before it read every file."""
    if run.backfill_complete:
        run.backfill_complete = False


def _note_gaps(run: RefreshRun) -> frozenset[str]:
    """Return the goalie files that earlier reads left as known gaps, and note them."""
    gaps = run.skipped(MONEYPUCK) - {TEAM_GAMES_URL}
    if gaps:
        run.notes.append(f"{MONEYPUCK}: {len(gaps)} files skipped, because every read failed")
    return gaps


def moneypuck(run: RefreshRun) -> None:
    """Read the team file and each goalie file that is missing or, for the team file and the
    current season, older than a week. A finished season is read once."""
    current = season_start_year(run.now.date())
    tried = 0
    if _stale(run, TEAM_GAMES_URL):
        # Only a file that was read counts, so a failing team file does not hold back the rest.
        tried += run.attempt(
            MONEYPUCK,
            TEAM_GAMES_URL,
            lambda stamp: _note_credit(
                run, write_team_games(run.conn, fetch_team_games(run.client), stamp)
            ),
        )
    failures = 0
    gaps = _note_gaps(run)
    for season in range(FIRST_SEASON, current + 1):
        url = goalie_games_url(season)
        if url in gaps:
            continue
        if failures >= MAX_UNIT_FAILURES:
            run.notes.append(
                f"{MONEYPUCK}: stopped after {failures} failed files; the rest not tried"
            )
            _mark_incomplete(run)
            break
        if not _goalie_file_stale(run, season, current):
            continue
        if _stop_for_time(run, f"the goalie file of season {season}", tried):
            break

        def unit(stamp: datetime, season: int = season) -> int:
            rows = fetch_goalie_games(run.client, season)
            return _note_credit(run, write_goalie_games(run.conn, rows, stamp))

        ok = run.attempt(MONEYPUCK, url, unit)
        tried += ok
        failures = 0 if ok else failures + 1


def _write_starters(run: RefreshRun, source: str, reads: list[StarterRead], stamp: datetime) -> int:
    result = write_starter_reads(run.conn, reads, stamp)
    if result.unmatched:
        shown = ", ".join(result.unmatched[:_UNMATCHED_SHOWN])
        run.notes.append(
            f"{source}: {len(result.unmatched)} goalie reads matched no stored game ({shown})"
        )
    return result.written


# The ESPN and Daily Faceoff reads ignore the time budget on purpose. Each is one small request,
# and the live goalie reads must run even when a backfill spent its time (#136).
def espn_starters(run: RefreshRun) -> None:
    """Read the ESPN probable goalies for today and tomorrow, as game days in Eastern time."""
    today = game_day_of(run.now)
    days: list[date] = [today, today + timedelta(days=1)]

    def unit(stamp: datetime) -> int:
        reads = [read for day in days for read in fetch_scoreboard_starters(run.client, day)]
        return _write_starters(run, ESPN_STARTERS, reads, stamp)

    run.attempt(ESPN_STARTERS, f"{ESPN_BASE}/scoreboard", unit)


def espn_injuries(run: RefreshRun) -> None:
    """Read the current ESPN injury list."""
    run.attempt(
        ESPN_INJURIES,
        f"{ESPN_BASE}/injuries",
        lambda stamp: write_injury_reads(run.conn, fetch_injuries(run.client), stamp),
    )


def daily_faceoff(run: RefreshRun) -> None:
    """Read the Daily Faceoff starting goalies."""
    run.attempt(
        DAILY_FACEOFF,
        STARTERS_URL,
        lambda stamp: _write_starters(run, DAILY_FACEOFF, fetch_starters(run.client), stamp),
    )
