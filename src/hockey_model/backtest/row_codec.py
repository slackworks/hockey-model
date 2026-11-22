"""The text form of training rows, for the season row cache.

The form is JSON, so a cache file that was damaged or edited fails with a named error and
never runs code. A float is written with ``repr``, which reads back to the same bits. A read
checks the type of every field, and refuses NaN and infinity, so a wrong value cannot pass as a
row.
Design: docs/design/backtest-speed.md
"""

import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hockey_model.game import DegradedInput, MissingInput, MissingKind
from hockey_model.game_model.features import FeatureRow
from hockey_model.game_model.training import TrainingRow
from hockey_model.sources.nhl import GameRow
from hockey_model.store.timestamps import iso_utc, parse_iso_utc
from hockey_model.types import HockeyModelError


def _game(row: GameRow) -> dict[str, Any]:
    return {
        "game_id": row.game_id,
        "season": row.season,
        "game_type": row.game_type,
        "start": iso_utc(row.start),
        "home": row.home,
        "away": row.away,
        "state": row.state,
        "home_score": row.home_score,
        "away_score": row.away_score,
        "last_period": row.last_period,
    }


def _features(row: FeatureRow) -> dict[str, Any]:
    return {
        "game_id": row.game_id,
        "elo_logit": row.elo_logit,
        "values": dict(row.values),
        "missing": [{"kind": m.kind.value, "team": m.team, "note": m.note} for m in row.missing],
        "uses_moneypuck": row.uses_moneypuck,
        "imputed": [
            {"feature": d.feature, "kind": d.kind.value, "teams": list(d.teams)}
            for d in row.imputed
        ],
    }


def encode_rows(rows: Sequence[TrainingRow]) -> str:
    """Return the text form of ``rows``."""
    data = [
        {"game": _game(r.game), "features": _features(r.features), "home_won": r.home_won}
        for r in rows
    ]
    return json.dumps(data, separators=(",", ":"), allow_nan=False)


def _typed(value: Any, kind: type, label: str) -> Any:
    # An exact type check, because a bool is an int in Python.
    if type(value) is not kind:
        raise TypeError(f"{label} must be {kind.__name__}, got {type(value).__name__}")
    return value


def _float(value: Any, label: str) -> float:
    number: float = _typed(value, float, label)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite, got {number}")
    return number


def _maybe(check: Callable[[Any, str], Any], value: Any, label: str) -> Any:
    return None if value is None else check(value, label)


def _int(value: Any, label: str) -> int:
    return _typed(value, int, label)


def _text(value: Any, label: str) -> str:
    return _typed(value, str, label)


def _decode_game(data: Mapping[str, Any]) -> GameRow:
    return GameRow(
        _int(data["game_id"], "game_id"),
        _int(data["season"], "season"),
        _int(data["game_type"], "game_type"),
        parse_iso_utc(_text(data["start"], "start")),
        _text(data["home"], "home"),
        _text(data["away"], "away"),
        _text(data["state"], "state"),
        _maybe(_int, data["home_score"], "home_score"),
        _maybe(_int, data["away_score"], "away_score"),
        _maybe(_text, data["last_period"], "last_period"),
    )


def _decode_missing(data: Mapping[str, Any]) -> MissingInput:
    return MissingInput(
        MissingKind(_text(data["kind"], "kind")),
        _maybe(_text, data["team"], "team"),
        _text(data["note"], "note"),
    )


def _decode_degraded(data: Mapping[str, Any]) -> DegradedInput:
    teams = _typed(data["teams"], list, "teams")
    return DegradedInput(
        _text(data["feature"], "feature"),
        MissingKind(_text(data["kind"], "kind")),
        tuple(_text(team, "team") for team in teams),
    )


def _decode_features(data: Mapping[str, Any]) -> FeatureRow:
    values = _typed(data["values"], dict, "values")
    return FeatureRow(
        _int(data["game_id"], "game_id"),
        _float(data["elo_logit"], "elo_logit"),
        {_text(name, "feature name"): _float(value, name) for name, value in values.items()},
        tuple(_decode_missing(m) for m in _typed(data["missing"], list, "missing")),
        _typed(data["uses_moneypuck"], bool, "uses_moneypuck"),
        tuple(_decode_degraded(d) for d in _typed(data["imputed"], list, "imputed")),
    )


def _decode_row(data: Mapping[str, Any]) -> TrainingRow:
    return TrainingRow(
        _decode_game(data["game"]),
        _decode_features(data["features"]),
        _typed(data["home_won"], bool, "home_won"),
    )


def _reject_constant(name: str) -> Any:
    # json.loads reads NaN and Infinity by default, and encode_rows never writes them.
    raise ValueError(f"{name} is not a number that a row holds")


def decode_rows(text: str) -> list[TrainingRow]:
    """Return the rows that ``encode_rows`` wrote, or raise a named error for a damaged text."""
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        raise HockeyModelError(
            f"decode season rows: the text is not valid JSON of rows: {exc}; delete the cache file"
        ) from exc
    if not isinstance(data, list):
        raise HockeyModelError(
            f"decode season rows: expected a list of rows, got {type(data).__name__}; "
            "delete the cache file"
        )
    rows: list[TrainingRow] = []
    for index, item in enumerate(data):
        try:
            rows.append(_decode_row(item))
        except (KeyError, TypeError, ValueError, AttributeError, HockeyModelError) as exc:
            raise HockeyModelError(
                f"decode season rows: row {index} is damaged ({type(exc).__name__}: {exc}); "
                "delete the cache file"
            ) from exc
    return rows
