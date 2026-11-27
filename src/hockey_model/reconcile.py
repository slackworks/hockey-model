"""Reconciliation checks of the play-by-play, the shifts, and the boxscore of a game (#35).

Each check is a pure function over plain data. It returns a result that names the check, says
whether it passed, and gives the values of a failure. A check never adds a player or a goal to
make itself pass. The status of the game follows the results and the revisit schedule.
Design: docs/design/pbp-shift-dataset.md
"""

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from hockey_model.stints import Stint, stints_around

SHOOTOUT = "SO"
# The position codes of the NHL roster: centre, left wing, right wing, defence, goalie.
POSITIONS = frozenset({"C", "L", "R", "D", "G"})
# A failed check lists at most this many items in its notes. The total is always kept.
MAX_LISTED = 10


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The result of one check: its name, whether it passed, and the values of a failure."""

    name: str
    passed: bool
    detail: dict[str, object]


@dataclass(frozen=True, slots=True)
class SituationEvent:
    """An event that the situation check reads: where it is and the code of the feed."""

    event_id: int
    period: int
    second: int
    code: str | None


def _expected_goals(
    home: str, away: str, scores: tuple[int | None, int | None], last_period: str | None
) -> dict[str, int] | str:
    """Return the goals that the play-by-play must hold, or the reason that none can be given."""
    home_score, away_score = scores
    if home_score is None or away_score is None:
        return "the game has no final score"
    expected = {home: home_score, away: away_score}
    if last_period == SHOOTOUT:
        if home_score == away_score:
            return "a shootout game has equal scores"
        expected[home if home_score > away_score else away] -= 1
    return expected


def check_goals_by_team(
    goals: Mapping[str, int],
    home: str,
    away: str,
    scores: tuple[int | None, int | None],
    last_period: str | None,
) -> CheckResult:
    """Goals by team, without shootout goals, equal the final score minus the shootout goal."""
    expected = _expected_goals(home, away, scores, last_period)
    if isinstance(expected, str):
        return CheckResult("goals_by_team", False, {"reason": expected})
    found = {home: goals.get(home, 0), away: goals.get(away, 0)}
    found.update({team: n for team, n in goals.items() if team not in (home, away)})
    return CheckResult("goals_by_team", found == expected, {"expected": expected, "found": found})


def check_goals_by_player(
    play_by_play: Mapping[int, int], boxscore: Mapping[int, int]
) -> CheckResult:
    """Goals by player in the play-by-play equal the goals of the boxscore."""
    differing = [
        {"player": p, "play_by_play": play_by_play.get(p, 0), "boxscore": boxscore.get(p, 0)}
        for p in sorted(play_by_play.keys() | boxscore.keys())
        if play_by_play.get(p, 0) != boxscore.get(p, 0)
    ]
    if not differing:
        return CheckResult("goals_by_player", True, {})
    detail = {"total": len(differing), "players": differing[:MAX_LISTED]}
    return CheckResult("goals_by_player", False, detail)


def check_roster(shift_players: Iterable[int], positions: Mapping[int, str]) -> CheckResult:
    """Each player with a shift is in the roster with a known position code.

    The builder takes a goalie from the position ``G``, and it counts any other player as a
    skater, so a roster that omits a goalie or names a strange code would hide a goalie.
    """
    on_ice = set(shift_players)
    missing = sorted(on_ice - positions.keys())
    unknown = [
        {"player": player, "position": positions[player]}
        for player in sorted(on_ice & positions.keys())
        if positions[player] not in POSITIONS
    ]
    passed = not missing and not unknown
    detail: dict[str, object] = {} if passed else {"missing": missing, "unknown_position": unknown}
    return CheckResult("roster", passed, detail)


def _skaters_of(code: str) -> tuple[int, int]:
    """Return the home and the away skaters that a situation code names."""
    return int(code[2]), int(code[1])


def _situation_failure(event: SituationEvent, stints: Sequence[Stint]) -> dict[str, object] | None:
    if event.code is None:
        return None
    around = stints_around(stints, event.period, event.second)
    if not around:
        return {"event": event.event_id, "reason": "no stint holds the event"}
    home, away = _skaters_of(event.code)
    if any(len(s.home_skaters) >= home and len(s.away_skaters) >= away for s in around):
        return None
    owner = around[0]
    return {
        "event": event.event_id,
        "expected": {"home": home, "away": away},
        "found": {"home": len(owner.home_skaters), "away": len(owner.away_skaters)},
    }


def check_situation(events: Sequence[SituationEvent], stints: Sequence[Stint]) -> CheckResult:
    """No event has fewer skaters on the ice than its situation code gives.

    The stint that owns an event, or the stint that starts at its second, may satisfy the code.
    A shootout event is not in the input, and an event with no code is not checked.
    """
    coded = [e for e in events if e.code is not None]
    if not coded:
        reason = "no event carries a situation code"
        return CheckResult("situation", False, {"reason": reason, "events": len(events)})
    failures = [f for e in coded if (f := _situation_failure(e, stints)) is not None]
    if not failures:
        return CheckResult("situation", True, {"checked": len(coded)})
    return CheckResult(
        "situation", False, {"total": len(failures), "events": failures[:MAX_LISTED]}
    )


def check_espn_teams(espn_teams: Collection[str], home: str, away: str) -> CheckResult:
    """The two teams of the ESPN summary are the two teams of the NHL game."""
    found, game = sorted(set(espn_teams)), sorted({home, away})
    return CheckResult(
        "espn_teams", found == game, {} if found == game else {"espn": found, "game": game}
    )


def status_of(results: Sequence[CheckResult], *, settled: bool) -> str:
    """Return ``provisional`` before the last revisit, then ``reconciled`` or ``incomplete``."""
    if not settled:
        return "provisional"
    return "reconciled" if all(r.passed for r in results) else "incomplete"


def check_shift_lengths(*, zero_length: int, negative_length: int) -> CheckResult:
    """No shift ends before it starts. A shift with no length is harmless and only counted."""
    detail: dict[str, object] = {"zero_length": zero_length, "negative_length": negative_length}
    return CheckResult("shift_lengths", negative_length == 0, detail)


def notes_json(results: Sequence[CheckResult]) -> str:
    """Return the notes of an event status: the result of each check."""
    entries = [{"check": r.name, "passed": r.passed, "detail": r.detail} for r in results]
    return json.dumps(entries, separators=(",", ":"))
