"""Turn the shifts of a game into stints: spans with one set of skaters on the ice.

A stint is a span of one period in which the players on the ice do not change. The module is
pure: it reads shift rows and a roster, and it makes no request and no query.
An event at second ``t`` belongs to the stint with ``start < t <= end``, so a goal at a line
change goes to the players who were on the ice before the change.
Design: docs/design/pbp-shift-dataset.md
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

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
    home_goalie: int | None
    away_goalie: int | None


def merge_intervals(
    intervals: Iterable[Interval],
) -> tuple[list[Interval], list[Interval]]:
    """Return the merged intervals, and the intervals that were dropped for having no length.

    Two intervals of one player in one period that overlap or touch become one. The result is
    sorted by period, player, and start, so equal input gives equal output.
    """
    dropped = [i for i in intervals if i.end <= i.start]
    by_player: dict[tuple[int, int, str], list[Interval]] = defaultdict(list)
    for interval in intervals:
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
            else:
                if current is not None:
                    merged.append(current)
                current = interval
        if current is not None:
            merged.append(current)
    return merged, dropped


def _side(
    on_ice: Sequence[Interval], team: str, goalies: frozenset[int], *, home: bool
) -> tuple[tuple[int, ...], int | None]:
    """Return the sorted skaters and the goalie of one side among the players on the ice."""
    ids = sorted(i.player_id for i in on_ice if (i.team == team) == home)
    skaters = tuple(p for p in ids if p not in goalies)
    netminders = [p for p in ids if p in goalies]
    return skaters, (netminders[0] if netminders else None)


def _span_players(
    intervals: Sequence[Interval], low: int, high: int, goalies: frozenset[int], home: str
) -> tuple[tuple[int, ...], tuple[int, ...], int | None, int | None]:
    """Return the home skaters, the away skaters, and the two goalies on the ice in a span."""
    on_ice = [i for i in intervals if i.start <= low and i.end >= high]
    skaters_home, goalie_home = _side(on_ice, home, goalies, home=True)
    skaters_away, goalie_away = _side(on_ice, home, goalies, home=False)
    return skaters_home, skaters_away, goalie_home, goalie_away


def _period_stints(
    period: int, intervals: Sequence[Interval], goalies: frozenset[int], home: str
) -> list[Stint]:
    points = sorted({i.start for i in intervals} | {i.end for i in intervals})
    stints: list[Stint] = []
    for low, high in zip(points, points[1:], strict=False):
        skaters_home, skaters_away, goalie_home, goalie_away = _span_players(
            intervals, low, high, goalies, home
        )
        if not (skaters_home or skaters_away or goalie_home or goalie_away):
            continue
        stint = Stint(period, low, high, skaters_home, skaters_away, goalie_home, goalie_away)
        previous = stints[-1] if stints else None
        if previous is not None and previous.end == low and _same_players(previous, stint):
            stints[-1] = Stint(
                period,
                previous.start,
                high,
                previous.home_skaters,
                previous.away_skaters,
                previous.home_goalie,
                previous.away_goalie,
            )
        else:
            stints.append(stint)
    return stints


def _same_players(a: Stint, b: Stint) -> bool:
    return (a.home_skaters, a.away_skaters, a.home_goalie, a.away_goalie) == (
        b.home_skaters,
        b.away_skaters,
        b.home_goalie,
        b.away_goalie,
    )


def build_stints(
    intervals: Iterable[Interval], positions: Mapping[int, str], home: str
) -> list[Stint]:
    """Return the stints of a game, in time order.

    ``intervals`` must be merged. ``positions`` maps a player id to a position code, and the
    players with the code ``G`` are the goalies. ``home`` is the home team code. A span with
    nobody on the ice, such as an intermission, is not a stint. A player that ``positions`` does
    not name counts as a skater, so the caller can see the gap in the roster check.
    """
    goalies = frozenset(player for player, position in positions.items() if position == GOALIE)
    by_period: dict[int, list[Interval]] = defaultdict(list)
    for interval in intervals:
        by_period[interval.period].append(interval)
    stints: list[Stint] = []
    for period in sorted(by_period):
        stints.extend(_period_stints(period, by_period[period], goalies, home))
    return stints


def stint_at(stints: Sequence[Stint], second: int) -> Stint | None:
    """Return the stint that owns an event at ``second``, or None when no stint does.

    A stint owns the seconds in ``start < second <= end``. An event at the very start of a
    period, at the start second of its first stint, belongs to that first stint.
    """
    for index, stint in enumerate(stints):
        if stint.start < second <= stint.end:
            return stint
        starts_period = index == 0 or stints[index - 1].period != stint.period
        if second == stint.start and starts_period:
            return stint
    return None
