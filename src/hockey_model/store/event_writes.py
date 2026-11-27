"""Write the rows that the play-by-play, shift chart, and ESPN summary parsers derive (#35).

Each derived row names the observation that gave it. A write replaces the rows of that one
observation, so a rebuild with a new parser gives the same rows as a first parse of the same
bytes. Design: docs/design/pbp-shift-dataset.md
"""

import sqlite3

from hockey_model.sources.espn_summary import SUMMARY_PARSER, EspnSummary
from hockey_model.sources.nhl_events import PBP_PARSER, PbpRows
from hockey_model.sources.nhl_shifts import SHIFTS_PARSER, ShiftRows


def write_pbp(conn: sqlite3.Connection, obs_id: int, rows: PbpRows) -> None:
    """Write the roster and the events of one play-by-play observation."""
    conn.execute("DELETE FROM pbp_roster WHERE obs_id = ?", (obs_id,))
    conn.execute("DELETE FROM pbp_events WHERE obs_id = ?", (obs_id,))
    conn.executemany(
        "INSERT INTO pbp_roster (obs_id, game_id, player_id, team, position) VALUES (?,?,?,?,?)",
        [(obs_id, rows.game_id, s.player_id, s.team, s.position) for s in rows.roster],
    )
    conn.executemany(
        "INSERT INTO pbp_events (obs_id, game_id, event_id, sort_order, period, period_type,"
        " period_second, game_second, type, situation_code, owner_team, x, y, attack_x_sign,"
        " zone, shot_type, player1, player2, player3, goalie_in_net, home_score, away_score,"
        " parser) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                obs_id,
                rows.game_id,
                e.event_id,
                e.sort_order,
                e.period,
                e.period_type,
                e.period_second,
                e.game_second,
                e.type,
                e.situation_code,
                e.owner_team,
                e.x,
                e.y,
                e.attack_x_sign,
                e.zone,
                e.shot_type,
                *e.players,
                e.goalie_in_net,
                e.home_score,
                e.away_score,
                PBP_PARSER,
            )
            for e in rows.events
        ],
    )


def write_shifts(conn: sqlite3.Connection, obs_id: int, rows: ShiftRows) -> None:
    """Write the shifts of one shift chart observation, in the order of the feed."""
    conn.execute("DELETE FROM shifts WHERE obs_id = ?", (obs_id,))
    conn.executemany(
        "INSERT INTO shifts (obs_id, game_id, row_no, player_id, team, period, start_second,"
        " end_second, shift_number, parser) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                obs_id,
                rows.game_id,
                number,
                s.player_id,
                s.team,
                s.period,
                s.start_second,
                s.end_second,
                s.shift_number,
                SHIFTS_PARSER,
            )
            for number, s in enumerate(rows.shifts)
        ],
    )


def write_espn(conn: sqlite3.Connection, obs_id: int, game_id: int, rows: EspnSummary) -> None:
    """Write the skater time on ice of one ESPN summary observation.

    The row keeps the ESPN name and no NHL id. A read step joins the name to the roster of the
    game, so a rebuild never depends on the player catalog of the day.
    """
    conn.execute("DELETE FROM espn_skater_toi WHERE obs_id = ?", (obs_id,))
    conn.executemany(
        "INSERT INTO espn_skater_toi (obs_id, game_id, team, espn_name, toi, pp_toi, sh_toi,"
        " es_toi, shifts, parser) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                obs_id,
                game_id,
                s.team,
                s.name,
                s.toi,
                s.pp_toi,
                s.sh_toi,
                s.es_toi,
                s.shifts,
                SUMMARY_PARSER,
            )
            for s in rows.skaters
        ],
    )
