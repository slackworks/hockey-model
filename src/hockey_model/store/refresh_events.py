"""The event steps of a refresh: play-by-play and shift charts of final games (#35).

One unit reads one game and one source, with the rules of a boxscore read: one transaction, a
stop after ``MAX_UNIT_FAILURES`` failed units, and a known gap after the existing failure rule.
The derive hook of a unit writes the rows and then reconciles the game, so the status changes in
the transaction of the input that changed it.
Design: docs/design/pbp-shift-dataset.md
"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from hockey_model import progress as progress_log
from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl_events import PBP_PARSER, fetch_pbp, pbp_url
from hockey_model.sources.nhl_shifts import SHIFTS_PARSER, fetch_shifts, shifts_url
from hockey_model.store.boxscore_revisit import REVISIT_AFTER_DAYS
from hockey_model.store.db import StoreError
from hockey_model.store.event_writes import write_pbp, write_shifts
from hockey_model.store.recording import Reading
from hockey_model.store.refresh_context import MAX_UNIT_FAILURES, RefreshRun
from hockey_model.store.refresh_nhl import RETRY_FAILED_AFTER
from hockey_model.store.refresh_revisit import revisit_source
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS, failed_since
from hockey_model.store.timestamps import iso_utc

# A plain refresh reads the first copy of a game that ended in the last days only. An older game
# without events is the work of the backfill.
FIRST_READ_DAYS = max(REVISIT_AFTER_DAYS)

Writer = Callable[[sqlite3.Connection, int], None]


@dataclass(frozen=True, slots=True)
class _Kind:
    """What differs between the sources: the parser, the URL, and the fetch."""

    parser: str
    url: Callable[[int], str]
    fetch: Callable[[HttpClient, int], Writer]


def _fetch_pbp(client: HttpClient, game_id: int) -> Writer:
    rows = fetch_pbp(client, game_id)
    return lambda conn, obs_id: write_pbp(conn, obs_id, rows)


def _fetch_shifts(client: HttpClient, game_id: int) -> Writer:
    rows = fetch_shifts(client, game_id)
    return lambda conn, obs_id: write_shifts(conn, obs_id, rows)


_KINDS = {
    NHL_PBP: _Kind(PBP_PARSER, pbp_url, _fetch_pbp),
    NHL_SHIFTS: _Kind(SHIFTS_PARSER, shifts_url, _fetch_shifts),
}


def _unit(run: RefreshRun, source: str, game_id: int) -> tuple[Callable[[datetime], int], Reading]:
    """Return the unit that reads one game of ``source``, and how its bytes are archived."""
    kind = _KINDS[source]
    held: list[Writer] = []

    def unit(_stamp: datetime) -> int:
        held.clear()
        held.append(kind.fetch(run.client, game_id))
        return 1

    def derive(conn: sqlite3.Connection, obs_id: int) -> None:
        if len(held) != 1:
            raise StoreError(
                f"derive {source} {game_id}: the unit ended with no parsed rows; "
                "the derive hook runs only after a good unit"
            )
        held[0](conn, obs_id)
        run.reconcile_in_unit(game_id)

    return unit, Reading(kind.parser, derive=derive)


def games_missing_events(run: RefreshRun, source: str) -> list[int]:
    """Return the final games of the last days with no good read of ``source``, oldest first."""
    since = iso_utc(run.now - timedelta(days=FIRST_READ_DAYS))
    rows = run.conn.execute(
        "SELECT g.game_id FROM latest_final_games g WHERE g.start_utc >= ?"
        " AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.source = ?"
        " AND o.key = CAST(g.game_id AS TEXT) AND o.status = 'ok') ORDER BY g.start_utc",
        (since, source),
    )
    return [int(game_id) for (game_id,) in rows]


def read_events(run: RefreshRun, source: str, game_ids: Sequence[int]) -> None:
    """Read ``source`` for each game in ``game_ids``. Each game is its own unit.

    The read stops after three failed units in a row, and when the time budget is spent. A game
    whose last read failed a day ago or less is left for the next refresh.
    """
    url_of = _KINDS[source].url
    gaps = run.skipped(source)
    failures = 0
    for index, game_id in enumerate(game_ids):
        left = len(game_ids) - index
        if failures >= MAX_UNIT_FAILURES:
            run.stop(f"{source}: stopped after {failures} failed reads; {left} not tried")
            return
        if run.out_of_time():
            run.stop(f"{source}: stopped, because the time ran out; {left} not tried")
            run.timed_out = True
            return
        url = url_of(game_id)
        if url in gaps or failed_since(run.conn, source, url, run.now - RETRY_FAILED_AFTER):
            progress_log.advance()
            continue
        unit, reading = _unit(run, source, game_id)
        failures = 0 if run.attempt(source, url, unit, reading) else failures + 1
        progress_log.advance()


def _read_source(run: RefreshRun, source: str) -> None:
    waiting = games_missing_events(run, source)
    with run.step(source, "missing", len(waiting)):
        read_events(run, source, waiting)
    revisit_source(run, source, _KINDS[source].url, lambda games: read_events(run, source, games))


def events(run: RefreshRun) -> None:
    """Read the play-by-play and the shift chart of each final game that lacks one, then revisit."""
    for source in _KINDS:
        _read_source(run, source)
