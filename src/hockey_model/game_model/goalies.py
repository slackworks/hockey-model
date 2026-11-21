"""Decide which goalie starts a game, and say how sure the answer is."""

import sqlite3
from collections import Counter
from datetime import datetime
from functools import cached_property

from hockey_model.game import (
    Game,
    GoalieStart,
    MissingInput,
    MissingKind,
    StarterSource,
    StarterStatus,
)
from hockey_model.game_model.names import matching_players, name_key
from hockey_model.identity import Catalog
from hockey_model.store.membership import goalie_membership_before
from hockey_model.store.reads import (
    StarterReadRow,
    franchise_codes,
    goalie_starts_before,
    injury_reads_before,
    injury_snapshot_before,
    player_catalog,
    starter_reads_before,
)
from hockey_model.store.source_reads import LIVE_FEED_MAX_AGE
from hockey_model.store.timestamps import iso_utc, parse_iso_utc
from hockey_model.types import HockeyModelError, Player, Slot

# The guess counts starts in this many of the newest games of the team.
STARTER_WINDOW = 10


class _Goalies:
    """The goalies of the store, found by name. The lists are built on first use."""

    def __init__(self, catalog: Catalog, team: str) -> None:
        self.catalog = catalog
        self._team_code = team

    @cached_property
    def league(self) -> list[Player]:
        return [p for p in self.catalog.players if p.eligible == frozenset({Slot.G})]

    @cached_property
    def team(self) -> list[Player]:
        codes = franchise_codes(self._team_code)
        return [p for p in self.league if p.team_code in codes]

    def find(self, name: str, *, league: bool = True) -> tuple[Player | None, str, bool]:
        """Return the goalie that ``name`` means, or ``None``, the reason, and ambiguity.

        A goalie that was traded may not have played for the new team yet, so a name
        that matches nobody on the team is looked up in the whole league, unless
        ``league`` is false. Two matches are never resolved by a guess.
        """
        if name_key(name) is None:
            return None, f"goalie name {name!r} has no first name or initial and a surname", False
        for pool in (self.team, self.league) if league else (self.team,):
            found = matching_players(name, pool)
            if len(found) == 1:
                return found[0], "", False
            if len(found) > 1:
                return None, f"goalie name {name!r} matches {len(found)} goalies", True
        return None, f"goalie name {name!r} matches no goalie in the store", False


def _unknown(team: str, note: str) -> MissingInput:
    return MissingInput(MissingKind.GOALIE_UNKNOWN, team, note)


def boxscore_starter(
    conn: sqlite3.Connection, game: Game, team: str, catalog: Catalog
) -> GoalieStart | None:
    row = conn.execute(
        "SELECT nhl_id, fetched_at FROM goalie_games WHERE game_id = ? AND team = ?"
        " AND started = 1",
        (game.game_id, team),
    ).fetchone()
    if row is None:
        return None
    return GoalieStart(
        catalog.resolve(row[0]),
        StarterStatus.CONFIRMED,
        StarterSource.NHL_BOXSCORE,
        parse_iso_utc(row[1]),
    )


def _start_from(read: StarterReadRow, player: Player) -> GoalieStart:
    source = StarterSource(read.source)
    return GoalieStart(player, read.status, source, read.fetched_at)


def _first_confirmed(
    kept: GoalieStart | None,
    read: StarterReadRow,
    player: Player,
    team: str,
    notes: list[MissingInput],
) -> GoalieStart:
    """Keep the first confirmed start, and note a later confirmation of another goalie."""
    if kept is None:
        return _start_from(read, player)
    if player.canonical_id != kept.player.canonical_id:
        text = (
            f"{kept.player.full_name} was confirmed first ({kept.source.value}, "
            f"{iso_utc(kept.read_at)}) and is used; {player.full_name} was confirmed later "
            f"({read.source}, {iso_utc(read.fetched_at)})"
        )
        notes.append(MissingInput(MissingKind.GOALIE_CONFLICT, team, text))
    return kept


def _newest_likely(
    kept: GoalieStart | None,
    read: StarterReadRow,
    player: Player,
    team: str,
    conflicts: list[MissingInput],
) -> GoalieStart:
    """Return the start of the newest likely read, and note when it replaces another goalie."""
    if kept is not None and player.canonical_id != kept.player.canonical_id:
        text = (
            f"{kept.player.full_name} was likely ({kept.source.value}, {iso_utc(kept.read_at)}); "
            f"{player.full_name} is likely later ({read.source}, {iso_utc(read.fetched_at)}) "
            "and is used"
        )
        conflicts.append(MissingInput(MissingKind.GOALIE_CONFLICT, team, text))
    return _start_from(read, player)


def _from_reads(
    reads: list[StarterReadRow], goalies: _Goalies, team: str
) -> tuple[GoalieStart | None, list[MissingInput]]:
    """Pick the earliest confirmed read, else the newest likely read.

    A conflict between likely reads shows only when no read is confirmed, because a confirmed
    read decides the start.
    """
    notes: list[MissingInput] = []
    conflicts: list[MissingInput] = []
    confirmed: GoalieStart | None = None
    likely: GoalieStart | None = None
    for read in reads:
        if read.status is StarterStatus.UNKNOWN:
            continue
        player, why, _ = goalies.find(read.goalie_name)
        if player is None:
            notes.append(_unknown(team, why))
        elif read.status is StarterStatus.CONFIRMED:
            confirmed = _first_confirmed(confirmed, read, player, team, notes)
        elif read.status is StarterStatus.LIKELY:
            likely = _newest_likely(likely, read, player, team, conflicts)
    if confirmed is not None:
        return confirmed, notes
    notes.extend(conflicts)
    if likely is not None:
        note = f"{likely.player.full_name} is likely, not confirmed"
        notes.append(MissingInput(MissingKind.GOALIE_UNCONFIRMED, team, note))
    return likely, notes


def _injured_goalies(
    conn: sqlite3.Connection, goalies: _Goalies, team: str, as_of: datetime
) -> tuple[set[int | None], list[MissingInput]]:
    """Return the ids of the team's goalies who are out, and a note for each unclear name."""
    out: set[int | None] = set()
    notes: list[MissingInput] = []
    snapshot = injury_snapshot_before(conn, as_of)
    if snapshot is not None and as_of - snapshot > LIVE_FEED_MAX_AGE:
        # An old list could still name a goalie who has healed, so no one is left out.
        note = f"the injury list was read at {iso_utc(snapshot)}, which is too old to use"
        notes.append(_unknown(team, note))
    for injury in injury_reads_before(conn, as_of):
        if not injury.is_out or injury.team not in franchise_codes(team):
            continue
        # Only the team's own goalies can be the guessed starter, so an injury name is not
        # looked up in the league: a skater could match goalies of other teams.
        player, why, ambiguous = goalies.find(injury.player_name, league=False)
        if player is not None:
            out.add(player.nhl_id)
        elif ambiguous:
            notes.append(_unknown(team, f"an injured player is unclear: {why}"))
    return out, notes


def _eligible(
    conn: sqlite3.Connection, goalies: _Goalies, team: str, as_of: datetime, ids: set[int]
) -> tuple[set[int], list[MissingInput]]:
    """Return the candidates that have not left the team, and a note for each unclear one.

    Only a guess is filtered. A source that names a goalie may know of a trade that the
    games do not show yet.
    """
    membership = goalie_membership_before(conn, team, ids, as_of)
    notes = [
        _unknown(
            team,
            f"{goalies.catalog.resolve(nhl_id).full_name} has conflicting teams at one start "
            "time; the guess may be wrong",
        )
        for nhl_id in sorted(membership.conflicting)
    ]
    return ids - membership.departed, notes


def _guess(
    conn: sqlite3.Connection, goalies: _Goalies, team: str, as_of: datetime
) -> tuple[GoalieStart | None, list[MissingInput]]:
    starts = goalie_starts_before(conn, team, as_of, STARTER_WINDOW)
    counts = Counter(nhl_id for _, nhl_id in starts)
    out, notes = _injured_goalies(conn, goalies, team, as_of)
    here, member_notes = _eligible(conn, goalies, team, as_of, set(counts) - out)
    order = {nhl_id: rank for rank, (_, nhl_id) in enumerate(reversed(starts))}
    ranked = sorted(here, key=lambda nhl_id: (-counts[nhl_id], -order[nhl_id]))
    notes = [*notes, *member_notes]
    if not ranked:
        if counts:
            notes.append(_unknown(team, "every goalie with a recent start is out or has left"))
        return None, notes
    player = goalies.catalog.resolve(ranked[0])
    return GoalieStart(player, StarterStatus.UNKNOWN, StarterSource.MODEL_GUESS, as_of), notes


def _given_goalie(
    game: Game, team: str, given: GoalieStart, as_of: datetime
) -> tuple[GoalieStart, tuple[MissingInput, ...]]:
    """Check a goalie that the caller put on ``game``, as a stored read is checked.

    The read must come before ``min(as_of, game.start)``. The comparison is strict, as in
    ``starter_reads_before``, so a read made at the cutoff is not known yet. The note is the
    one that a stored read of the same status would give.
    """
    cutoff = min(as_of, game.start)
    if given.read_at >= cutoff:
        side = "home" if team == game.home else "away"
        raise HockeyModelError(
            f"the {side} goalie for game {game.game_id} was read at {iso_utc(given.read_at)}, "
            f"which is not before the cutoff {iso_utc(cutoff)}; "
            "give a start that was read before as_of and before the game began"
        )
    name = given.player.full_name
    if given.status is StarterStatus.LIKELY:
        note = f"{name} is likely, not confirmed"
        return given, (MissingInput(MissingKind.GOALIE_UNCONFIRMED, team, note),)
    if given.status is StarterStatus.UNKNOWN:
        return given, (_unknown(team, f"{name} was supplied with an unknown status"),)
    return given, ()


def resolve_goalie(
    conn: sqlite3.Connection,
    game: Game,
    team: str,
    as_of: datetime,
    *,
    use_reads: bool,
    use_boxscore: bool = True,
    catalog: Catalog | None = None,
) -> tuple[GoalieStart | None, tuple[MissingInput, ...]]:
    """Return the goalie for ``team`` in ``game`` at ``as_of``, and what was missing.

    The order is: a goalie on ``game``, which must have been read before the cutoff; the
    boxscore starter when the game has started; the earliest confirmed read; the newest
    likely read; the goalie with the most starts in the last ``STARTER_WINDOW`` games.
    With ``use_reads`` false, only the first two and the last apply.
    With ``use_boxscore`` false, the boxscore of a game that has started is ignored,
    so a backtest can ask what a morning guess would have been.
    A caller that resolves many games passes one ``catalog`` to reuse.
    """
    given = game.home_goalie if team == game.home else game.away_goalie
    if given is not None:
        return _given_goalie(game, team, given, as_of)
    catalog = player_catalog(conn) if catalog is None else catalog
    if use_boxscore and as_of >= game.start:
        started = boxscore_starter(conn, game, team, catalog)
        if started is not None:
            note = "prediction made after puck drop; the goalie is the boxscore starter"
            return started, (MissingInput(MissingKind.LATE_RUN, team, note),)
    goalies = _Goalies(catalog, team)
    notes: list[MissingInput] = []
    if use_reads:
        chosen, notes = _from_reads(
            starter_reads_before(conn, game.game_id, team, as_of), goalies, team
        )
        if chosen is not None:
            return chosen, tuple(notes)
    guessed, injury_notes = _guess(conn, goalies, team, as_of)
    notes = [*notes, *injury_notes]
    why = "no source names the starter; the goalie with the most recent starts is used"
    if guessed is None:
        why = "no source names the starter, and the store has no goalie start for the team"
    return guessed, (*notes, _unknown(team, why))
