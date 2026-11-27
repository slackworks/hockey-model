"""Build the stints of a stored game, run the reconciliation checks, and store the status (#35).

The status is ``provisional`` until both observations were read after the last revisit day of
the game. Then it is ``reconciled`` when every check passed, and ``incomplete`` when one failed.
Design: docs/design/pbp-shift-dataset.md
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from hockey_model.reconcile import (
    CheckResult,
    SituationEvent,
    check_espn_teams,
    check_goals_by_player,
    check_goals_by_team,
    check_roster,
    check_situation,
    notes_json,
    status_of,
)
from hockey_model.store.archive import OK, SHAPE_ERROR
from hockey_model.store.boxscore_revisions import LATEST, boxscore_rows
from hockey_model.store.refresh_nhl import REVISIT_AFTER_DAYS
from hockey_model.store.source_reads import ESPN_SUMMARY
from hockey_model.store.stint_build import GameInputs, GameStints, build_game_stints
from hockey_model.store.timestamps import iso_utc, parse_iso_utc


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
    goals: dict[str, int] = {}
    for owner, _ in _goals(conn, inputs.pbp_obs):
        if owner is not None:
            goals[owner] = goals.get(owner, 0) + 1
    scores, last_period = _final_score(conn, inputs.game_id)
    return check_goals_by_team(goals, inputs.home, inputs.away, scores, last_period)


def _player_check(conn: sqlite3.Connection, inputs: GameInputs) -> CheckResult:
    revision = boxscore_rows(conn, inputs.game_id, policy=LATEST)
    if revision is None:
        return CheckResult("goals_by_player", False, {"reason": "the game has no boxscore"})
    scored: dict[int, int] = {}
    for _, scorer in _goals(conn, inputs.pbp_obs):
        if scorer is not None and inputs.positions.get(scorer) != "G":
            scored[scorer] = scored.get(scorer, 0) + 1
    boxed = {s.nhl_id: s.goals for s in revision.skaters if s.goals > 0}
    return check_goals_by_player(scored, boxed)


def _situation_check(conn: sqlite3.Connection, built: GameStints) -> CheckResult:
    rows = conn.execute(
        "SELECT event_id, period, game_second, situation_code FROM pbp_events"
        " WHERE obs_id = ? AND game_second IS NOT NULL ORDER BY sort_order",
        (built.pbp_obs,),
    ).fetchall()
    events = [SituationEvent(*row) for row in rows]
    return check_situation(events, built.stints.stints)


def _espn_check(conn: sqlite3.Connection, inputs: GameInputs) -> CheckResult | None:
    """Check the teams of the newest ESPN summary of the game, or return None when it has none."""
    teams = conn.execute(
        "SELECT DISTINCT t.team FROM espn_skater_toi t WHERE t.obs_id = ("
        " SELECT o.obs_id FROM observations o"
        " WHERE o.source = ? AND o.key = ? AND o.status IN (?, ?)"
        " ORDER BY o.fetched_at DESC, o.obs_id DESC LIMIT 1)",
        (ESPN_SUMMARY, str(inputs.game_id), OK, SHAPE_ERROR),
    ).fetchall()
    if not teams:
        return None
    return check_espn_teams([team for (team,) in teams], inputs.home, inputs.away)


def _is_settled(conn: sqlite3.Connection, inputs: GameInputs) -> bool:
    """Return True when both observations were read after the last revisit day of the game."""
    start = conn.execute(
        "SELECT start_utc FROM games WHERE game_id = ? ORDER BY fetched_at DESC LIMIT 1",
        (inputs.game_id,),
    ).fetchone()
    if start is None or start[0] is None:
        return False
    last = parse_iso_utc(start[0]) + timedelta(days=max(REVISIT_AFTER_DAYS))
    read = conn.execute(
        "SELECT MIN(fetched_at) FROM observations WHERE obs_id IN (?, ?)",
        (inputs.pbp_obs, inputs.shift_obs),
    ).fetchone()[0]
    return parse_iso_utc(read) >= last


def _checks(conn: sqlite3.Connection, built: GameStints, inputs: GameInputs) -> list[CheckResult]:
    results = [
        _team_check(conn, inputs),
        _player_check(conn, inputs),
        check_roster((i.player_id for i in inputs.intervals), inputs.positions),
        _situation_check(conn, built),
    ]
    espn = _espn_check(conn, inputs)
    return results if espn is None else [*results, espn]


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


def reconcile_game(conn: sqlite3.Connection, game_id: int, now: datetime) -> GameStatus | None:
    """Build the stints of a game, check them, and store the status. None means no inputs.

    The caller owns the transaction. A second call for the same pair of observations replaces
    the stints and the status of that pair.
    """
    built = build_game_stints(conn, game_id)
    if built is None:
        return None
    inputs = built.inputs
    results = _checks(conn, built, inputs)
    status = status_of(results, settled=_is_settled(conn, inputs))
    game = GameStatus(game_id, built.pbp_obs, built.shift_obs, status, tuple(results))
    notes = notes_json(results, dropped_shifts=len(built.stints.dropped))
    _write_status(conn, game, notes, now)
    return game
