"""Save and load fitted models, and select one by identity or by label."""

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from hockey_model.game_model.features import feature_name_problem
from hockey_model.game_model.logistic import FitDiagnostics, LogisticFit
from hockey_model.store.db import StoreError
from hockey_model.store.timestamps import iso_utc, parse_iso_utc

# The label that `hockey-model game` and `hockey-model daily` serve. A research fit that
# carries no label cannot replace it, which is what #80 asks for.
LIVE_LABEL = "live"


class UnknownFitError(StoreError):
    """Raised when a fit id names no saved fit."""


@dataclass(frozen=True, slots=True)
class FitRef:
    """A request for one saved fit, by its id or by a label. Set exactly one field."""

    fit_id: int | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if (self.fit_id is None) == (self.label is None):
            raise ValueError("a FitRef sets exactly one of fit_id or label")
        if self.fit_id is not None and self.fit_id <= 0:
            raise ValueError(f"fit_id must be positive, got {self.fit_id}")
        if self.label is not None and not self.label.strip():
            raise ValueError("label must not be blank")


@dataclass(frozen=True, slots=True)
class FitArtifact:
    """One saved fit with the provenance a reader needs to name it.

    ``fit_through_season`` is None only for a fit saved before the column existed. A reader
    that compares a game season against it MUST treat None as unknown, not as out of sample.
    """

    fit_id: int
    fit: LogisticFit
    fit_through_season: int | None
    created_at: datetime
    labels: tuple[str, ...] = ()

    def covers_season(self, season: int) -> bool | None:
        """Return whether ``season`` trained this fit, or None when the cutoff is unknown."""
        if self.fit_through_season is None:
            return None
        return season <= self.fit_through_season


@dataclass(frozen=True, slots=True)
class FitRecord:
    """One fit as it is saved: the weights, the season it trained through, and its label.

    ``imputation`` is the imputed count and share of each selected feature, as the fit was
    built. It is stored beside the weights, so a reader can see how much of the fit was a
    stand-in. A fit with no features passes an empty list.

    ``label`` names the fit for later selection. It is written in the same transaction as the
    fit row, so a reader never sees the label without the fit that it names.
    """

    fit: LogisticFit
    fit_through_season: int
    created_at: datetime
    imputation: Sequence[Mapping[str, object]] = ()
    label: str | None = None


def save_fit(conn: sqlite3.Connection, record: FitRecord) -> int:
    """Store ``record`` and return the new fit id. An invalid fit saves no row."""
    fit = record.fit
    try:
        _check_fit(fit)
    except ValueError as exc:
        raise StoreError(f"save the model fit: the fit cannot be used ({exc})") from exc
    weights: dict[str, object] = {
        "intercept": fit.intercept,
        "elo_weight": fit.elo_weight,
        "features": dict(fit.weights),
        "imputation": _imputation_entries(record.imputation),
    }
    if fit.diagnostics is not None:
        weights["diagnostics"] = asdict(fit.diagnostics)
    with conn:
        cursor = conn.execute(
            "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
            " VALUES (?,?,?,?)",
            (
                record.fit_through_season,
                json.dumps(list(fit.features)),
                json.dumps(weights, sort_keys=True),
                iso_utc(record.created_at),
            ),
        )
        if cursor.lastrowid is None:
            raise StoreError("save the model fit: the database gave no row id; check the store")
        if record.label is not None:
            _insert_label(conn, cursor.lastrowid, record.label, record.created_at)
    return cursor.lastrowid


def load_fit(conn: sqlite3.Connection, ref: FitRef) -> FitArtifact:
    """Return the fit that ``ref`` names, with its provenance.

    An unknown id raises ``UnknownFitError``. A label that names no fit, and a store with no
    saved fit, raise ``StoreError`` that names the fix. No caller gets the newest fit by
    default: #80 found that a research fit saved to a local store would then be served.
    """
    fit_id = ref.fit_id if ref.fit_id is not None else _fit_id_of_label(conn, ref.label or "")
    row = conn.execute(
        "SELECT fit_through_season, features, weights, created_at FROM model_fits WHERE fit_id = ?",
        (fit_id,),
    ).fetchone()
    if row is None:
        raise UnknownFitError(
            f"load the model fit: fit {fit_id} is not in the store; "
            "save the fit first with `hockey-model fit`"
        )
    through, features, weights, created_at = row
    return FitArtifact(
        fit_id,
        _build_fit(features, weights),
        None if through is None else int(through),
        parse_iso_utc(created_at),
        _labels_of(conn, fit_id),
    )


def label_fit(conn: sqlite3.Connection, fit_id: int, label: str, set_at: datetime) -> None:
    """Record that ``label`` serves fit ``fit_id`` from ``set_at``. The newest row wins."""
    with conn:
        _insert_label(conn, fit_id, label, set_at)


def _insert_label(conn: sqlite3.Connection, fit_id: int, label: str, set_at: datetime) -> None:
    """Add one label row. A blank or padded label is refused, because a reader types it."""
    if not label or label != label.strip():
        raise ValueError(
            f"label must not be blank or carry spaces around it, got {label!r}; "
            "give a name such as 'live'"
        )
    known = conn.execute("SELECT 1 FROM model_fits WHERE fit_id = ?", (fit_id,)).fetchone()
    if known is None:
        raise UnknownFitError(f"label fit {fit_id} as {label!r}: fit {fit_id} is not in the store")
    conn.execute(
        "INSERT INTO fit_labels (label, fit_id, set_at) VALUES (?,?,?)",
        (label, fit_id, iso_utc(set_at)),
    )


def _fit_id_of_label(conn: sqlite3.Connection, label: str) -> int:
    """Return the fit that ``label`` serves now, or raise a ``StoreError`` that names the fix."""
    row = conn.execute(
        "SELECT fit_id FROM fit_labels WHERE label = ? ORDER BY rowid DESC LIMIT 1", (label,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    if conn.execute("SELECT 1 FROM model_fits LIMIT 1").fetchone() is None:
        raise StoreError(
            "load the model fit: no fit is saved; run `hockey-model fit --label live` first"
        )
    raise StoreError(
        f"load the model fit: no fit carries the label {label!r}; "
        f"save one with `hockey-model fit --label {label}`"
    )


def _labels_of(conn: sqlite3.Connection, fit_id: int) -> tuple[str, ...]:
    """Return every label that serves ``fit_id`` now, in label order."""
    rows = conn.execute(
        "SELECT label, fit_id FROM fit_labels WHERE fit_id = ? ORDER BY label", (fit_id,)
    )
    return tuple(label for label, _ in rows)


def _build_fit(features: str, weights: str) -> LogisticFit:
    """Rebuild the saved fit. A row that does not parse raises a ``StoreError`` with the fix."""
    try:
        names = tuple(json.loads(features))
        stored = json.loads(weights)
        fit = LogisticFit(
            names,
            float(stored["intercept"]),
            float(stored["elo_weight"]),
            {name: float(value) for name, value in stored["features"].items()},
            _diagnostics(stored.get("diagnostics")),
        )
        _check_fit(fit)
        return fit
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StoreError(
            f"load the model fit: the saved fit is malformed ({type(exc).__name__}: {exc}); "
            "run `hockey-model fit` again"
        ) from exc


_IMPUTATION_KEYS = frozenset({"feature", "imputed", "total", "share"})


def _imputation_entries(entries: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return the imputation entries as plain dicts. A bad entry raises ``StoreError``."""
    for entry in entries:
        if set(entry) != _IMPUTATION_KEYS:
            raise StoreError(
                f"save the model fit: an imputation entry needs exactly the keys "
                f"{sorted(_IMPUTATION_KEYS)}, got {sorted(entry)}"
            )
    return [dict(entry) for entry in entries]


def _check_fit(fit: LogisticFit) -> None:
    """Raise ``ValueError`` for a fit that the model cannot predict with."""
    problem = feature_name_problem(fit.features)
    if problem is not None:
        raise ValueError(problem)
    if set(fit.weights) != set(fit.features):
        raise ValueError("the features and the weights name different features")
    numbers = [fit.intercept, fit.elo_weight, *fit.weights.values()]
    if not all(math.isfinite(number) for number in numbers):
        raise ValueError("a weight is not a finite number")


def _diagnostics(stored: dict[str, Any] | None) -> FitDiagnostics | None:
    """Rebuild the saved diagnostics. A fit saved before they existed has none."""
    if stored is None:
        return None
    return FitDiagnostics(
        iterations=int(stored["iterations"]),
        objective=float(stored["objective"]),
        gradient_norm=float(stored["gradient_norm"]),
        condition=float(stored["condition"]),
        l2=float(stored["l2"]),
        means={str(k): float(v) for k, v in stored["means"].items()},
        scales={str(k): float(v) for k, v in stored["scales"].items()},
        stop_reason=str(stored["stop_reason"]),
    )
