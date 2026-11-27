"""Build the stints of a stored game, run the reconciliation checks, and store the status (#35).

The status is ``provisional`` until both observations were read after the last revisit day of
the game. Then it is ``reconciled`` when every check passed, and ``incomplete`` when one failed.
Design: docs/design/pbp-shift-dataset.md
"""

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from hockey_model.reconcile import (
    CheckResult,
    SituationEvent,
    check_espn_teams,
    check_game_state,
    check_goals_by_player,
    check_goals_by_team,
    check_roster,
    check_shift_lengths,
    check_situation,
    notes_json,
    status_of,
)
from hockey_model.stints import GOALIE
from hockey_model.store.archive import OK, SHAPE_ERROR
from hockey_model.store.boxscore_revisions import (
    COMPLETE,
    LATEST,
    MISMATCHED,
    boxscore_rows,
)
from hockey_model.store.boxscore_revisit import REVISIT_AFTER_DAYS
from hockey_model.store.db import StoreError
from hockey_model.store.source_reads import ESPN_SUMMARY, NHL_PBP, NHL_SHIFTS
from hockey_model.store.stint_build import (
    GameInputs,
    GameStints,
    build_game_stints,
    missing_inputs,
    write_stints,
)
from hockey_model.store.timestamps import iso_utc, parse_iso_utc
from hockey_model.types import HockeyModelError

# An NHL game id is ten ASCII digits. isdigit() also accepts other scripts.
_GAME_KEY = re.compile(r"[0-9]{10}")


@dataclass(frozen=True, slots=True)
class GameStatus:
    """The status of one pair of observations, and the result of each check."""

    game_id: int
    pbp_obs: int
    shift_obs: int
    status: str
    results: tuple[CheckResult, ...]


def _final_score(
    conn: sqlite3.Connection, game_id: int
) -> tuple[tuple[int | None, int | None], str | None]:
    found = conn.execute(
        "SELECT home_score, away_score, last_period FROM latest_final_games WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    return ((None, None), None) if found is None else ((found[0], found[1]), found[2])


def _goals(conn: sqlite3.Connection, pbp_obs: int) -> list[tuple[str | None, int | None]]:
    """Return the owner team and the scorer of each goal that is not a shootout goal."""
    return conn.execute(
        "SELECT owner_team, player1 FROM pbp_events"
        " WHERE obs_id = ? AND type = 'goal' AND period_type != 'SO'",
        (pbp_obs,),
    ).fetchall()


def _team_check(conn: sqlite3.Connection, inputs: GameInputs) -> CheckResult:
    goals: dict[str | None, int] = {}
    for owner, _ in _goals(conn, inputs.pbp_obs):
        goals[owner] = goals.get(owner, 0) + 1
    scores, last_period = _final_score(conn, inputs.game_id)
    return check_goals_by_team(goals, inputs.home, inputs.away, scores, last_period)


def _player_check(conn: sqlite3.Connection, inputs: GameInputs) -> CheckResult:
    revision = boxscore_rows(conn, inputs.game_id, policy=LATEST)
    if revision is None:
        return CheckResult("goals_by_player", False, {"reason": "the game has no boxscore"})
    if revision.completeness != COMPLETE:
        reason = "the newest boxscore revision is incomplete"
        return CheckResult("goals_by_player", False, {"reason": reason})
    if revision.identity == MISMATCHED:
        reason = "the newest boxscore revision names another game or teams"
        return CheckResult("goals_by_player", False, {"reason": reason})
    scored: dict[int, int] = {}
    for _, scorer in _goals(conn, inputs.pbp_obs):
        if scorer is not None and inputs.positions.get(scorer) != GOALIE:
            scored[scorer] = scored.get(scorer, 0) + 1
    boxed = {s.nhl_id: s.goals for s in revision.skaters if s.goals > 0}
    return check_goals_by_player(scored, boxed)


def _situation_check(conn: sqlite3.Connection, built: GameStints) -> CheckResult:
    rows = conn.execute(
        "SELECT event_id, period, game_second, situation_code FROM pbp_events"
        " WHERE obs_id = ? AND game_second IS NOT NULL ORDER BY sort_order",
        (built.inputs.pbp_obs,),
    ).fetchall()
    events = [SituationEvent(*row) for row in rows]
    return check_situation(events, built.stints.stints)


def _state_check(conn: sqlite3.Connection, inputs: GameInputs) -> CheckResult:
    found = conn.execute(
        "SELECT DISTINCT game_state FROM pbp_events WHERE obs_id = ?", (inputs.pbp_obs,)
    )
    return check_game_state({state for (state,) in found})


def _espn_check(conn: sqlite3.Connection, inputs: GameInputs) -> CheckResult:
    """Check the teams of the newest ESPN summary of the game.

    A game with no summary has no check to run, and the notes say so. A newest summary that was
    refused has no rows, and the check fails, because an older summary would hide the refusal.
    """
    newest = conn.execute(
        "SELECT o.obs_id FROM observations o"
        " WHERE o.source = ? AND o.key = ? AND o.status IN (?, ?)"
        " ORDER BY o.fetched_at DESC, o.obs_id DESC LIMIT 1",
        (ESPN_SUMMARY, str(inputs.game_id), OK, SHAPE_ERROR),
    ).fetchone()
    if newest is None:
        skipped = {"skipped": "no ESPN summary is stored for the game"}
        return CheckResult("espn_teams", True, skipped)
    teams = conn.execute(
        "SELECT DISTINCT team FROM espn_skater_toi WHERE obs_id = ?", (newest[0],)
    ).fetchall()
    if not teams:
        reason = {"reason": "the newest ESPN summary has no rows"}
        return CheckResult("espn_teams", False, reason)
    return check_espn_teams([team for (team,) in teams], inputs.home, inputs.away)


def _stored_time(game_id: int, text: str) -> datetime:
    """Parse a time that the store wrote, or raise a store error that names the game."""
    try:
        return parse_iso_utc(text)
    except (ValueError, HockeyModelError) as exc:
        raise StoreError(
            f"game {game_id}: the stored time {text!r} does not parse ({exc}); "
            "fix the row, or read the game again"
        ) from exc


def _is_settled(conn: sqlite3.Connection, inputs: GameInputs) -> bool:
    """Return True when every input was read after the last revisit day of the game.

    The inputs are the play-by-play, the shift chart, and the newest boxscore revision. A game
    with no boxscore revision fails the player check, so it is judged on the other two.
    """
    start = conn.execute(
        "SELECT start_utc FROM games WHERE game_id = ? ORDER BY fetched_at DESC LIMIT 1",
        (inputs.game_id,),
    ).fetchone()
    if start is None or start[0] is None:
        return False
    last = _stored_time(inputs.game_id, start[0]) + timedelta(days=max(REVISIT_AFTER_DAYS))
    reads = conn.execute(
        "SELECT fetched_at FROM observations WHERE obs_id IN (?, ?)",
        (inputs.pbp_obs, inputs.shift_obs),
    )
    times = [_stored_time(inputs.game_id, text) for (text,) in reads]
    revision = boxscore_rows(conn, inputs.game_id, policy=LATEST)
    if revision is not None:
        times.append(revision.fetched_at)
    return min(times) >= last


def _checks(conn: sqlite3.Connection, built: GameStints, inputs: GameInputs) -> list[CheckResult]:
    results = [
        _team_check(conn, inputs),
        _player_check(conn, inputs),
        check_roster((i.player_id for i in inputs.intervals), inputs.positions),
        check_shift_lengths(
            zero_length=built.stints.zero_length, negative_length=built.stints.negative_length
        ),
        _situation_check(conn, built),
        _state_check(conn, inputs),
    ]
    return [*results, _espn_check(conn, inputs)]


def _write_status(
    conn: sqlite3.Connection, game: GameStatus, notes: str, checked_at: datetime
) -> None:
    conn.execute(
        "DELETE FROM event_status WHERE game_id = ? AND pbp_obs = ? AND shift_obs = ?",
        (game.game_id, game.pbp_obs, game.shift_obs),
    )
    conn.execute(
        "INSERT INTO event_status (game_id, pbp_obs, shift_obs, status, checked_at, notes)"
        " VALUES (?,?,?,?,?,?)",
        (game.game_id, game.pbp_obs, game.shift_obs, game.status, iso_utc(checked_at), notes),
    )


def _forget(conn: sqlite3.Connection, game_id: int) -> None:
    """Delete every stint and every status of a game."""
    for table in ("stints", "event_status"):
        conn.execute(f"DELETE FROM {table} WHERE game_id = ?", (game_id,))


def reconcile_game(conn: sqlite3.Connection, game_id: int, now: datetime) -> GameStatus | None:
    """Build the stints of a game, check them, and store the status. None means no inputs.

    The caller owns the transaction. A second call for the same pair of observations replaces
    the stints and the status of that pair and removes those of an older pair. A call that
    raises a store error deletes the stints and the status of the game, because a status that its
    inputs no longer support is a claim.
    """
    try:
        built = build_game_stints(conn, game_id)
        if built is None:
            return None
        inputs = built.inputs
        results = _checks(conn, built, inputs)
        status = status_of(results, settled=_is_settled(conn, inputs))
    except StoreError:
        # No status means that the game is unknown. The old one must not outlive inputs that
        # no longer reconcile.
        _forget(conn, game_id)
        raise
    game = GameStatus(game_id, inputs.pbp_obs, inputs.shift_obs, status, tuple(results))
    # The views read the newest pair only. The rows of an older pair are derived rows that a
    # run of this function makes again, so they go instead of growing with each revisit.
    _forget(conn, game_id)
    write_stints(conn, built)
    _write_status(conn, game, notes_json(results), now)
    return game


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What ``reconcile_games`` did: the games it read and the status that each one got.

    ``skipped`` and ``failed`` hold one line for each game, and the line names the game and the
    reason.
    """

    games: int
    provisional: int
    reconciled: int
    incomplete: int
    skipped: tuple[str, ...]
    failed: tuple[str, ...]


def _game_ids(conn: sqlite3.Connection) -> tuple[list[int], list[str]]:
    """Return the game ids of the event observations, and a line for each key that is not one."""
    ids: set[int] = set()
    bad: list[str] = []
    found = conn.execute(
        "SELECT obs_id, source, key FROM observations WHERE source IN (?, ?) AND key IS NOT NULL"
        " ORDER BY obs_id",
        (NHL_PBP, NHL_SHIFTS),
    )
    for obs_id, source, key in found:
        if _GAME_KEY.fullmatch(key) is None:
            bad.append(f"observation {obs_id} of {source}: key {key!r} is not a game id")
        else:
            ids.add(int(key))
    return sorted(ids), bad


def reconcile_games(
    conn: sqlite3.Connection, now: datetime, game_ids: Sequence[int] | None = None
) -> ReconcileReport:
    """Reconcile each named game, or each game with a play-by-play or a shift chart.

    A game with no inputs is skipped with its reason, and a game that raises a store error is
    named in ``failed``, so one bad game does not stop the rest. The run is one transaction.
    """
    ids, failed = _game_ids(conn) if game_ids is None else (list(game_ids), [])
    counts = {"provisional": 0, "reconciled": 0, "incomplete": 0}
    skipped: list[str] = []
    with conn:
        for game_id in ids:
            try:
                game = reconcile_game(conn, game_id, now)
                if game is None:
                    skipped.append(f"game {game_id}: {missing_inputs(conn, game_id)}")
                else:
                    counts[game.status] += 1
            except StoreError as exc:
                failed.append(str(exc))
    return ReconcileReport(
        len(ids),
        counts["provisional"],
        counts["reconciled"],
        counts["incomplete"],
        tuple(skipped),
        tuple(failed),
    )
