"""The text form of training rows, for the season row cache.

The form is JSON, so a cache file that was damaged or edited fails with a named error and
never runs code. A float is written with ``repr``, which reads back to the same bits.
Design: docs/design/backtest-speed.md
"""

import json
from collections.abc import Mapping, Sequence
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


def _decode_game(data: Mapping[str, Any]) -> GameRow:
    return GameRow(
        data["game_id"],
        data["season"],
        data["game_type"],
        parse_iso_utc(data["start"]),
        data["home"],
        data["away"],
        data["state"],
        data["home_score"],
        data["away_score"],
        data["last_period"],
    )


def _decode_features(data: Mapping[str, Any]) -> FeatureRow:
    return FeatureRow(
        data["game_id"],
        data["elo_logit"],
        dict(data["values"]),
        tuple(MissingInput(MissingKind(m["kind"]), m["team"], m["note"]) for m in data["missing"]),
        data["uses_moneypuck"],
        tuple(
            DegradedInput(d["feature"], MissingKind(d["kind"]), tuple(d["teams"]))
            for d in data["imputed"]
        ),
    )


def _decode_row(data: Mapping[str, Any]) -> TrainingRow:
    return TrainingRow(
        _decode_game(data["game"]), _decode_features(data["features"]), data["home_won"]
    )


def decode_rows(text: str) -> list[TrainingRow]:
    """Return the rows that ``encode_rows`` wrote, or raise a named error for a damaged text."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HockeyModelError(
            f"decode season rows: the text is not JSON: {exc}; delete the cache file"
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
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise HockeyModelError(
                f"decode season rows: row {index} is damaged ({type(exc).__name__}: {exc}); "
                "delete the cache file"
            ) from exc
    return rows
