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
from datetime import timedelta
from functools import partial

from hockey_model import progress as progress_log
from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl_events import PBP_PARSER, fetch_pbp, pbp_url
from hockey_model.sources.nhl_shifts import SHIFTS_PARSER, fetch_shifts, shifts_url
from hockey_model.store.boxscore_revisit import REVISIT_AFTER_DAYS
from hockey_model.store.event_unit import Writer, event_unit
from hockey_model.store.event_writes import write_pbp, write_shifts
from hockey_model.store.refresh_context import MAX_UNIT_FAILURES, RefreshRun
from hockey_model.store.refresh_nhl import RETRY_FAILED_AFTER
from hockey_model.store.refresh_revisit import revisit_source
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS, failed_since
from hockey_model.store.timestamps import iso_utc

# A plain refresh reads the first copy of a game that ended in the last days only. An older game
# without events is the work of the backfill.
FIRST_READ_DAYS = max(REVISIT_AFTER_DAYS)

# A refresh reconciles at most this many games that have events and no status.
MAX_PENDING_RECONCILES = 100


@dataclass(frozen=True, slots=True)
class _Kind:
    """What differs between the sources: the parser, the URL, and the fetch."""

    parser: str
    url: Callable[[int], str]
    fetch: Callable[[int, HttpClient], Writer]


def _fetch_pbp(game_id: int, client: HttpClient) -> Writer:
    rows = fetch_pbp(client, game_id)
    return lambda conn, obs_id: write_pbp(conn, obs_id, rows)


def _fetch_shifts(game_id: int, client: HttpClient) -> Writer:
    rows = fetch_shifts(client, game_id)
    return lambda conn, obs_id: write_shifts(conn, obs_id, rows)


_KINDS = {
    NHL_PBP: _Kind(PBP_PARSER, pbp_url, _fetch_pbp),
    NHL_SHIFTS: _Kind(SHIFTS_PARSER, shifts_url, _fetch_shifts),
}


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
    kind = _KINDS[source]
    url_of = kind.url
    gaps = run.skipped(source)
    failures = 0
    gapped = waiting = 0
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
        if url in gaps:
            gapped += 1
        elif failed_since(run.conn, source, url, run.now - RETRY_FAILED_AFTER):
            waiting += 1
        else:
            unit, reading = event_unit(
                run, source, game_id, kind.parser, partial(kind.fetch, game_id)
            )
            before = run.unpublished
            ok = run.attempt(source, url, unit, reading)
            # The host answered for a feed that is not published yet, so the read is no failure.
            failures = failures if run.unpublished > before else (0 if ok else failures + 1)
        progress_log.advance()
    _note_skips(run, source, gapped, waiting)


def _note_skips(run: RefreshRun, source: str, gapped: int, waiting: int) -> None:
    """Name the games that a read left aside, so a game that stays out of the dataset is seen."""
    if gapped:
        run.notes.append(f"{source}: {gapped} game(s) skipped, because every read failed")
    if waiting:
        run.notes.append(
            f"{source}: {waiting} game(s) skipped, because the last read failed a day ago or less"
        )


def games_pending_reconcile(conn: sqlite3.Connection) -> list[int]:
    """Return the games with a good play-by-play and shift chart but no status for that pair.

    A game that could not reconcile has its status removed. It is not due for a read, so this
    list brings it back until it reconciles, and each run names the cause again in the notes.
    """
    rows = conn.execute(
        "SELECT CAST(p.key AS INTEGER) FROM observations p"
        " JOIN latest_event_observations lp ON lp.obs_id = p.obs_id"
        " JOIN observations s ON s.source = ? AND s.key = p.key AND s.status = 'ok'"
        " JOIN latest_event_observations ls ON ls.obs_id = s.obs_id"
        " WHERE p.source = ? AND p.status = 'ok' AND NOT EXISTS ("
        "SELECT 1 FROM event_status e WHERE e.pbp_obs = p.obs_id AND e.shift_obs = s.obs_id)"
        " ORDER BY p.key",
        (NHL_SHIFTS, NHL_PBP),
    )
    return [int(game_id) for (game_id,) in rows]


def _reconcile_pending(run: RefreshRun) -> None:
    """Reconcile the pending games, at most ``MAX_PENDING_RECONCILES`` and inside the budget."""
    pending = games_pending_reconcile(run.conn)
    if len(pending) > MAX_PENDING_RECONCILES:
        run.notes.append(
            f"reconcile: {len(pending) - MAX_PENDING_RECONCILES} game(s) wait for the next "
            f"refresh; this run reconciles at most {MAX_PENDING_RECONCILES}"
        )
    for index, game_id in enumerate(pending[:MAX_PENDING_RECONCILES]):
        if run.out_of_time():
            left = min(len(pending), MAX_PENDING_RECONCILES) - index
            run.stop(f"reconcile: stopped, because the time ran out; {left} not tried")
            run.timed_out = True
            return
        with run.conn:
            run.reconcile_in_unit(game_id)


def _read_source(run: RefreshRun, source: str) -> None:
    waiting = games_missing_events(run, source)
    with run.step(source, "missing", len(waiting)):
        read_events(run, source, waiting)
    revisit_source(run, source, _KINDS[source].url, lambda games: read_events(run, source, games))


def events(run: RefreshRun) -> None:
    """Read the play-by-play and the shift chart of each final game that lacks one, then revisit.

    A game with both reads and no status is reconciled again at the end.
    """
    for source in _KINDS:
        _read_source(run, source)
    _reconcile_pending(run)
