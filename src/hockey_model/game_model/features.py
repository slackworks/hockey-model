"""The features of one game: Elo plus expected-goals form, goalie form, and rest.

Every feature is the home value minus the away value. A value that cannot be computed is
0.0 and adds a ``MissingInput``, so a missing input never looks like a measured zero.
All history is read as of the earlier of ``as_of`` and the game start.
"""

import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from hockey_model.game import DegradedInput, Game, GoalieStart, MissingInput, MissingKind
from hockey_model.game_model.elo import EloBook
from hockey_model.game_model.goalies import boxscore_starter, resolve_goalie
from hockey_model.identity import Catalog
from hockey_model.store.reads import (
    mp_attempt_form_before,
    mp_goalie_form_before,
    mp_team_form_before,
    player_catalog,
    team_games_before,
)

# The production candidates. The attempt set replaces only xg_form, for the leak test (#77).
FEATURES = ("xg_form", "goalie_gsax", "rest")
ATTEMPT_FEATURES = ("attempt_form", "goalie_gsax", "rest")
TeamFormReader = Callable[[sqlite3.Connection, str, datetime, int], Sequence[tuple[float, float]]]
_FORM_READERS: dict[str, tuple[TeamFormReader, MissingKind]] = {
    "xg_form": (mp_team_form_before, MissingKind.XG_FORM),
    "attempt_form": (mp_attempt_form_before, MissingKind.ATTEMPT_FORM),
}
ALL_FEATURE_NAMES = ("xg_form", "attempt_form", "goalie_gsax", "rest")
GOALIE_FEATURE = "goalie_gsax"
FORM_WINDOW = 10
GOALIE_WINDOW = 20
REST_CAP_DAYS = 4.0
# The value of a form feature when either team has no form: no difference between the teams.
# The rest differential follows the same rule: a team with no earlier game in the store gives
# no difference, as a goalie with no rate does (#66, #144).
NEUTRAL_FORM = 0.0
_DAY = timedelta(days=1)


class GoalieMode(StrEnum):
    """Where the starting goalie comes from."""

    ACTUAL = "actual"  # the boxscore starter, which only a backtest can know
    READS = "reads"  # the source reads, then the estimate: a live prediction
    GUESS = "guess"  # the estimate only, even after puck drop: the cost of a morning guess


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """The model inputs for one game."""

    game_id: int
    elo_logit: float
    values: Mapping[str, float]
    missing: tuple[MissingInput, ...]
    uses_moneypuck: bool
    imputed: tuple[DegradedInput, ...] = ()


def feature_name_problem(names: Sequence[str]) -> str | None:
    """Return why ``names`` is not a usable feature list, or ``None`` when it is."""
    unknown = [name for name in names if name not in ALL_FEATURE_NAMES]
    if unknown:
        return f"unknown feature {unknown[0]!r}; use any of {', '.join(ALL_FEATURE_NAMES)}"
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        return f"feature {repeated[0]!r} is named more than once"
    return None


def _logit(probability: float) -> float:
    return math.log(probability / (1.0 - probability))


def _share(rows: Sequence[tuple[float, float]]) -> tuple[float | None, int]:
    """Return the mean share of the games with a total above zero, and the count left out.

    A game with a zero total has no share. A team whose every game has a zero total gets no share.
    """
    shares = [made / (made + allowed) for made, allowed in rows if made + allowed > 0]
    mean = sum(shares) / len(shares) if shares else None
    return mean, len(rows) - len(shares)


def _share_form(
    conn: sqlite3.Connection,
    game: Game,
    cutoff: datetime,
    read: TeamFormReader,
    kind: MissingKind,
) -> tuple[float, list[MissingInput], list[MissingInput]]:
    """Return home minus away share of ``read``'s made-for over its total.

    The second list holds the teams with no share, which make the difference neutral. The third
    list holds the teams that have a share although a game with a zero total was left out.
    """
    shares: dict[str, float | None] = {}
    gaps: list[MissingInput] = []
    left_out: list[MissingInput] = []
    for team in (game.home, game.away):
        rows = read(conn, team, cutoff, FORM_WINDOW)
        share, skipped = _share(rows)
        shares[team] = share
        if share is None:
            reason = (
                f"every one of {skipped} MoneyPuck games before this game has a zero total"
                if skipped
                else "no MoneyPuck games before this game"
            )
            gaps.append(MissingInput(kind, team, reason))
        elif skipped:
            note = f"{skipped} of {len(rows)} MoneyPuck games have a zero total and are left out"
            left_out.append(MissingInput(MissingKind.FORM_GAMES_LEFT_OUT, team, note))
    home, away = shares[game.home], shares[game.away]
    if home is None or away is None:
        return NEUTRAL_FORM, gaps, left_out
    return home - away, gaps, left_out


def _starter(
    conn: sqlite3.Connection,
    game: Game,
    team: str,
    as_of: datetime,
    mode: GoalieMode,
    catalog: Catalog,
) -> tuple[GoalieStart | None, list[MissingInput]]:
    if mode is GoalieMode.ACTUAL:
        start = boxscore_starter(conn, game, team, catalog)
        if start is not None:
            return start, []
        note = "the game has no boxscore starter"
        return None, [MissingInput(MissingKind.GOALIE_UNKNOWN, team, note)]
    start, notes = resolve_goalie(
        conn,
        game,
        team,
        as_of,
        use_reads=mode is GoalieMode.READS,
        use_boxscore=mode is not GoalieMode.GUESS,
        catalog=catalog,
    )
    return start, list(notes)


def _goalie_gsax(
    conn: sqlite3.Connection,
    game: Game,
    as_of: datetime,
    mode: GoalieMode,
    catalog: Catalog,
) -> tuple[float, list[MissingInput], bool]:
    """Return the home minus away goals saved above expected, the gaps, and MoneyPuck use.

    A side that has no rate makes the whole differential ``NEUTRAL_FORM``. Otherwise the other
    side's rate would pass through as a measured difference.
    """
    cutoff = min(as_of, game.start)
    missing: list[MissingInput] = []
    values: dict[str, float] = {}
    used = False
    for team in (game.home, game.away):
        start, notes = _starter(conn, game, team, as_of, mode, catalog)
        missing.extend(notes)
        values[team] = 0.0
        if start is None:
            note = f"no starting goalie for {team}, so goalie form is not used"
            missing.append(MissingInput(MissingKind.GOALIE_FORM, team, note))
            continue
        if start.player.nhl_id is None:
            note = f"{start.player.full_name} has no NHL id, so goalie form is not used"
            missing.append(MissingInput(MissingKind.GOALIE_FORM, team, note))
            continue
        rows = mp_goalie_form_before(conn, start.player.nhl_id, cutoff, GOALIE_WINDOW)
        if not rows:
            note = f"no MoneyPuck games for {start.player.full_name} before this game"
            missing.append(MissingInput(MissingKind.GOALIE_FORM, team, note))
            continue
        used = True
        values[team] = sum(xg - goals for xg, goals in rows) / len(rows)
    if any(m.kind is MissingKind.GOALIE_FORM for m in missing):
        # The neutral value uses no MoneyPuck number, even when the other side had one.
        return NEUTRAL_FORM, missing, False
    return values[game.home] - values[game.away], missing, used


def _days_rested(
    conn: sqlite3.Connection, team: str, start: datetime, cutoff: datetime
) -> float | None:
    """Return the days since the last game known at ``cutoff``, capped, or ``None``."""
    previous = team_games_before(conn, team, cutoff, 1)
    if not previous:
        return None
    return min((start - previous[0].start) / _DAY, REST_CAP_DAYS)


def _rest(
    conn: sqlite3.Connection, game: Game, cutoff: datetime
) -> tuple[float, list[MissingInput]]:
    """Return home minus away rest. A team with no earlier game makes the difference neutral.

    The store has no information about the rest of that team, so the known side does not pass
    through as a difference against a guessed rest (#144).
    """
    days: dict[str, float | None] = {}
    missing: list[MissingInput] = []
    for team in (game.home, game.away):
        found = _days_rested(conn, team, game.start, cutoff)
        if found is None:
            note = "no earlier game in the store; the rest difference is neutral"
            missing.append(MissingInput(MissingKind.REST_UNKNOWN, team, note))
        days[team] = found
    home, away = days[game.home], days[game.away]
    if home is None or away is None:
        return NEUTRAL_FORM, missing
    return home - away, missing


def _degraded(name: str, kind: MissingKind, gaps: Sequence[MissingInput]) -> DegradedInput:
    return DegradedInput(name, kind, tuple(m.team for m in gaps if m.team))


def _degraded_if(
    name: str, kind: MissingKind, missing: Sequence[MissingInput]
) -> tuple[DegradedInput, ...]:
    """Return the degraded input of ``name`` from the notes of ``kind``, or an empty tuple."""
    gaps = [m for m in missing if m.kind is kind]
    return (_degraded(name, kind, gaps),) if gaps else ()


def build_features(
    conn: sqlite3.Connection,
    game: Game,
    as_of: datetime,
    elo: EloBook,
    *,
    goalie_mode: GoalieMode,
    catalog: Catalog | None = None,
    names: Sequence[str],
) -> FeatureRow:
    """Return the features of ``game`` as known at ``as_of``.

    ``names`` picks the form features to read, so a candidate set that does not
    use a form does not read it or report its gaps. ``elo`` must hold the ratings
    after every game that was over at the cutoff. A caller that builds many rows
    passes one ``catalog`` to reuse.
    """
    problem = feature_name_problem(names)
    if problem is not None:
        raise ValueError(f"build_features: {problem}")
    catalog = player_catalog(conn) if catalog is None else catalog
    cutoff = min(as_of, game.start)
    forms = {
        name: _share_form(conn, game, cutoff, reader, kind)
        for name, (reader, kind) in _FORM_READERS.items()
        if name in names
    }
    gsax, goalie_missing, goalie_used = _goalie_gsax(conn, game, as_of, goalie_mode, catalog)
    if GOALIE_FEATURE not in names:
        # The starter notes stay, because a reader wants to know who plays. The goalie form
        # notes and the MoneyPuck use belong to a model that reads that feature.
        goalie_missing = [m for m in goalie_missing if m.kind is not MissingKind.GOALIE_FORM]
        goalie_used = False
    rest, rest_missing = _rest(conn, game, cutoff)
    # Two teams, so fewer than two gaps means the form was read for at least one team.
    form_used = any(len(gaps) < 2 for _, gaps, _ in forms.values())  # noqa: PLR2004
    values = {name: value for name, (value, _, _) in forms.items()}
    values.update({"goalie_gsax": gsax, "rest": rest})
    missing = [m for _, gaps, left_out in forms.values() for m in (*gaps, *left_out)]
    imputed = tuple(
        _degraded(name, _FORM_READERS[name][1], gaps)
        for name, (_, gaps, _) in forms.items()
        if gaps
    )
    # A goalie or a rest value that was not measured is set to a stand-in for that team.
    if GOALIE_FEATURE in names:
        imputed += _degraded_if(GOALIE_FEATURE, MissingKind.GOALIE_FORM, goalie_missing)
    if "rest" in names:
        imputed += _degraded_if("rest", MissingKind.REST_UNKNOWN, rest_missing)
    return FeatureRow(
        game_id=game.game_id,
        elo_logit=_logit(elo.home_win_chance(game.home, game.away)),
        values=values,
        missing=(*missing, *goalie_missing, *rest_missing),
        uses_moneypuck=form_used or goalie_used,
        imputed=imputed,
    )
