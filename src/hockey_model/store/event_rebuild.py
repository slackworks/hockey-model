"""Parse the archived play-by-play, shift charts, and ESPN summaries again (#35).

A rebuild reads stored payloads only and makes no request. It rewrites the derived rows of each
observation with the current parser, so a parser fix needs no new fetch. The rows of an
observation after a rebuild equal the rows of a first parse of the same bytes.
Design: docs/design/pbp-shift-dataset.md
"""

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from hockey_model.sources.espn_summary import SUMMARY_PARSER, parse_summary
from hockey_model.sources.nhl_events import PBP_PARSER, parse_pbp
from hockey_model.sources.nhl_shifts import SHIFTS_PARSER, parse_shifts
from hockey_model.sources.payload import get_json_text
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.store.archive import (
    OK,
    SHAPE_ERROR,
    Observation,
    observations_for_source,
    payload_bytes,
)
from hockey_model.store.boxscore_rebuild import RebuildReport
from hockey_model.store.db import StoreError
from hockey_model.store.event_writes import write_espn, write_pbp, write_shifts
from hockey_model.store.source_reads import ESPN_SUMMARY, NHL_PBP, NHL_SHIFTS

_EVENT = re.compile(r"[?&]event=([0-9]{1,12})\Z")
# An NHL game id is ten ASCII digits. isdigit() also accepts other scripts.
_GAME_KEY = re.compile(r"[0-9]{10}")


@dataclass(frozen=True, slots=True)
class _Kind:
    """How one event source parses a payload and writes the rows."""

    parser: str
    apply: Callable[[sqlite3.Connection, Observation, object], None]
    tables: tuple[str, ...]


def _game_of(obs: Observation) -> int:
    """Return the NHL game id that the observation is keyed on."""
    if obs.key is None or _GAME_KEY.fullmatch(obs.key) is None:
        raise StoreError(
            f"rebuild {obs.source} observation {obs.obs_id}: the key {obs.key!r} is not a game "
            "id; the observation was written without its game"
        )
    return int(obs.key)


def _apply_pbp(conn: sqlite3.Connection, obs: Observation, payload: object) -> None:
    write_pbp(conn, obs.obs_id, parse_pbp(payload, _game_of(obs)))


def _apply_shifts(conn: sqlite3.Connection, obs: Observation, payload: object) -> None:
    write_shifts(conn, obs.obs_id, parse_shifts(payload, _game_of(obs)))


def _apply_espn(conn: sqlite3.Connection, obs: Observation, payload: object) -> None:
    found = _EVENT.search(obs.url)
    if found is None:
        raise SourceParseError(ESPN_SUMMARY, f"observation {obs.obs_id} url names no ESPN event")
    write_espn(conn, obs.obs_id, _game_of(obs), parse_summary(payload, found[1]))


KINDS = {
    NHL_PBP: _Kind(PBP_PARSER, _apply_pbp, ("pbp_events", "pbp_roster")),
    NHL_SHIFTS: _Kind(SHIFTS_PARSER, _apply_shifts, ("shifts",)),
    ESPN_SUMMARY: _Kind(SUMMARY_PARSER, _apply_espn, ("espn_skater_toi",)),
}


def _payload(conn: sqlite3.Connection, obs: Observation, sha256: str) -> object:
    try:
        raw = payload_bytes(conn, sha256)
    except StoreError as exc:
        raise StoreError(f"rebuild {obs.source} observation {obs.obs_id}: {exc}") from exc
    return get_json_text(obs.source, raw, f"archived payload of observation {obs.obs_id}")


def _invalidate(conn: sqlite3.Connection, source: str, obs: Observation) -> None:
    """Drop the stints and the statuses that were built from the rows of ``obs``.

    A rebuild rewrites the rows of an observation in place, so a stint or a status that names the
    observation would describe rows that no longer exist. The next ``store reconcile`` makes
    them again. An ESPN summary feeds the status of its game only.
    """
    if source == ESPN_SUMMARY:
        if obs.key is not None and _GAME_KEY.fullmatch(obs.key):
            conn.execute("DELETE FROM event_status WHERE game_id = ?", (int(obs.key),))
        return
    for table in ("stints", "event_status"):
        conn.execute(
            f"DELETE FROM {table} WHERE pbp_obs = ? OR shift_obs = ?", (obs.obs_id, obs.obs_id)
        )


def _has_rows(conn: sqlite3.Connection, kind: _Kind, obs_id: int) -> bool:
    """Return True when an earlier parse left rows for the observation."""
    found = conn.execute(f"SELECT 1 FROM {kind.tables[0]} WHERE obs_id = ? LIMIT 1", (obs_id,))
    return found.fetchone() is not None


def rebuild_events(
    conn: sqlite3.Connection, source: str, parser: str, *, allow_refused: bool = False
) -> RebuildReport:
    """Rewrite the derived rows of each archived observation of ``source``.

    ``parser`` must name the parser that the code holds. A payload that an earlier read or an
    earlier parse accepted, and the parser refuses now, stops the rebuild as a parser regression
    does, unless ``allow_refused`` says that the stricter parser is intended. An earlier parse
    left rows for the observation. The rebuild is one transaction.
    """
    kind = KINDS[source]
    if parser != kind.parser:
        raise StoreError(
            f"rebuild {source}: parser {parser!r} is not the parser of this code; "
            f"pass --parser {kind.parser}"
        )
    seen = derived = 0
    refusals: list[str] = []
    with conn:
        for obs in observations_for_source(conn, source):
            if obs.sha256 is None or obs.status not in (OK, SHAPE_ERROR):
                continue
            seen += 1
            _invalidate(conn, source, obs)
            accepted_before = obs.status == OK or _has_rows(conn, kind, obs.obs_id)
            try:
                kind.apply(conn, obs, _payload(conn, obs, obs.sha256))
            except (SourceParseError, SourceShapeError) as exc:
                if accepted_before and not allow_refused:
                    raise StoreError(
                        f"rebuild {source} observation {obs.obs_id}: the parser {kind.parser} "
                        f"refuses a payload that an earlier read or parse accepted ({exc}); fix "
                        "the parser, or pass --allow-refused when the stricter parser is intended"
                    ) from exc
                # The rows of an earlier parse must not outlive a refusal: a view that picks the
                # newest observation would serve them as the rows of these bytes.
                for table in kind.tables:
                    conn.execute(f"DELETE FROM {table} WHERE obs_id = ?", (obs.obs_id,))
                refusals.append(f"observation {obs.obs_id}: {exc}")
                continue
            derived += 1
    return RebuildReport(source, parser, seen, derived, 0, len(refusals), tuple(refusals))
