"""Turn the shifts of a game into stints: spans with one set of players on the ice (#35).

A stint is a span of one period in which the players on the ice do not change. The module is
pure: it reads shift intervals and a roster, and it makes no request and no query.
An event at second ``t`` belongs to the stint with ``start < t <= end``, so a goal at a line
change goes to the players who were on the ice before the change.
Design: docs/design/pbp-shift-dataset.md
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from hockey_model.types import HockeyModelError

GOALIE = "G"


@dataclass(frozen=True, slots=True)
class Interval:
    """One player on the ice from ``start`` to ``end``, in game seconds of one period."""

    player_id: int
    team: str
    period: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Stint:
    """A span of one period with one set of players on the ice."""

    period: int
    start: int
    end: int
    home_skaters: tuple[int, ...]
    away_skaters: tuple[int, ...]
    home_goalies: tuple[int, ...]
    away_goalies: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Stints:
    """The stints of a game in time order, and the counts of the intervals that were dropped.

    A zero length interval is harmless. An interval that ends before it starts is a fault of the
    feed or of the clock parser."""

    stints: tuple[Stint, ...]
    zero_length: int
    negative_length: int


def merge_intervals(intervals: Iterable[Interval]) -> tuple[list[Interval], list[Interval]]:
    """Return the merged intervals, and the intervals that were dropped for having no length.

    Two intervals of one player in one period that overlap or touch become one. The result is
    sorted by period, player, team, and start, so equal input gives equal output.
    """
    rows = list(intervals)
    dropped = [i for i in rows if i.end <= i.start]
    by_player: dict[tuple[int, int, str], list[Interval]] = defaultdict(list)
    for interval in rows:
        if interval.end > interval.start:
            by_player[(interval.period, interval.player_id, interval.team)].append(interval)
    merged: list[Interval] = []
    for key in sorted(by_player):
        current: Interval | None = None
        for interval in sorted(by_player[key], key=lambda i: (i.start, i.end)):
            if current is not None and interval.start <= current.end:
                current = Interval(
                    current.player_id,
                    current.team,
                    current.period,
                    current.start,
                    max(current.end, interval.end),
                )
                continue
            if current is not None:
                merged.append(current)
            current = interval
        if current is not None:
            merged.append(current)
    return merged, dropped


def _stint(
    span: tuple[int, int, int], on_ice: Iterable[Interval], goalies: frozenset[int], home: str
) -> Stint:
    """Return the stint of the span ``(period, low, high)`` from the intervals on the ice."""
    sides: dict[bool, list[int]] = {True: [], False: []}
    netminders: dict[bool, list[int]] = {True: [], False: []}
    for interval in on_ice:
        target = netminders if interval.player_id in goalies else sides
        target[interval.team == home].append(interval.player_id)
    return Stint(
        *span,
        tuple(sorted(sides[True])),
        tuple(sorted(sides[False])),
        tuple(sorted(netminders[True])),
        tuple(sorted(netminders[False])),
    )


def _same_players(a: Stint, b: Stint) -> bool:
    return (a.home_skaters, a.away_skaters, a.home_goalies, a.away_goalies) == (
        b.home_skaters,
        b.away_skaters,
        b.home_goalies,
        b.away_goalies,
    )


def _period_stints(
    period: int, intervals: Sequence[Interval], goalies: frozenset[int], home: str
) -> list[Stint]:
    """Sweep the boundaries of one period. The intervals of a player must be merged."""
    opens: dict[int, list[Interval]] = defaultdict(list)
    closes: dict[int, list[Interval]] = defaultdict(list)
    for interval in intervals:
        opens[interval.start].append(interval)
        closes[interval.end].append(interval)
    points = sorted(opens.keys() | closes.keys())
    active: dict[tuple[int, str], Interval] = {}
    stints: list[Stint] = []
    for low, high in zip(points, points[1:], strict=False):
        for closed in closes.get(low, ()):
            del active[(closed.player_id, closed.team)]
        for opened in opens.get(low, ()):
            active[(opened.player_id, opened.team)] = opened
        if not active:
            continue
        stint = _stint((period, low, high), active.values(), goalies, home)
        previous = stints[-1] if stints else None
        if previous is not None and previous.end == low and _same_players(previous, stint):
            stints[-1] = Stint(period, previous.start, high, *_players(previous))
        else:
            stints.append(stint)
    return stints


def _players(stint: Stint) -> tuple[tuple[int, ...], ...]:
    return stint.home_skaters, stint.away_skaters, stint.home_goalies, stint.away_goalies


def build_stints(
    intervals: Iterable[Interval], positions: Mapping[int, str], home: str, away: str
) -> Stints:
    """Return the stints of a game, in time order, and the counts of the intervals dropped.

    The builder merges the intervals first. ``positions`` maps a player id to a position code,
    and the players with the code ``G`` are the goalies. A player that ``positions`` does not
    name counts as a skater, so the reconciliation can see the gap in the roster. A span with
    nobody on the ice, such as an intermission, is not a stint. An interval of a team that is
    neither ``home`` nor ``away`` is an error.
    """
    merged, dropped = merge_intervals(intervals)
    for interval in merged:
        if interval.team not in (home, away):
            raise HockeyModelError(
                f"player {interval.player_id} has a shift for team {interval.team!r}, which is "
                f"neither the home team {home} nor the away team {away}; check the team codes "
                "of the shift chart"
            )
    goalies = frozenset(player for player, position in positions.items() if position == GOALIE)
    by_period: dict[int, list[Interval]] = defaultdict(list)
    for interval in merged:
        by_period[interval.period].append(interval)
    stints: list[Stint] = []
    for period in sorted(by_period):
        stints.extend(_period_stints(period, by_period[period], goalies, home))
    negative = sum(1 for i in dropped if i.end < i.start)
    return Stints(tuple(stints), len(dropped) - negative, negative)


def stint_at(stints: Sequence[Stint], period: int, second: int) -> Stint | None:
    """Return the stint that owns an event of ``period`` at game ``second``, or None.

    A stint owns the seconds in ``start < second <= end``. An event at the first second of a
    period, which is the start of its first stint, belongs to that first stint.
    """
    first_start: int | None = None
    for stint in stints:
        if stint.period != period:
            continue
        if first_start is None:
            first_start = stint.start
            if second == first_start:
                return stint
        if stint.start < second <= stint.end:
            return stint
    return None


def stints_around(stints: Sequence[Stint], period: int, second: int) -> tuple[Stint, ...]:
    """Return the stint that owns an event, and the stint that starts at the event second.

    A faceoff after a penalty ends takes the situation code of the players after the change, and
    the owner rule gives it the players before the change. The two stints let a check accept
    either. The tuple is empty when no stint owns the event.
    """
    owner = stint_at(stints, period, second)
    if owner is None:
        return ()
    after = next(
        (s for s in stints if s.period == period and s.start == second != owner.start), None
    )
    return (owner,) if after is None else (owner, after)
