"""Tests for fit selection by identity or label, and the provenance the loader returns (#80)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from hockey_model.consumer_errors import UnknownFitError
from hockey_model.game_model.logistic import ELO_ONLY, LogisticFit
from hockey_model.store.db import StoreError
from hockey_model.store.fits import FitRecord, FitRef, label_fit, load_fit, save_fit

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _fit(feature: str = "rest") -> LogisticFit:
    return LogisticFit((feature,), 0.1, 0.9, {feature: 0.02})


def test_a_ref_needs_exactly_one_field() -> None:
    with pytest.raises(ValueError, match="exactly one of fit_id or label"):
        FitRef()
    with pytest.raises(ValueError, match="exactly one of fit_id or label"):
        FitRef(fit_id=1, label="live")


@pytest.mark.parametrize("fit_id", [0, -1])
def test_a_fit_id_must_be_positive(fit_id: int) -> None:
    with pytest.raises(ValueError, match="fit_id must be positive"):
        FitRef(fit_id=fit_id)


@pytest.mark.parametrize("label", ["", "   "])
def test_a_label_must_not_be_blank(label: str) -> None:
    with pytest.raises(ValueError, match="label must not be blank"):
        FitRef(label=label)


def test_load_by_id_returns_the_fit_and_its_provenance(store: sqlite3.Connection) -> None:
    fit = _fit()
    fit_id = save_fit(store, FitRecord(fit, 20252026, T0, label="live"))
    artifact = load_fit(store, FitRef(fit_id=fit_id))
    assert artifact.fit == fit
    assert artifact.fit_id == fit_id
    assert artifact.fit_through_season == 20252026
    assert artifact.created_at == T0
    assert artifact.labels == ("live",)


def test_load_by_label_returns_the_newest_fit_with_that_label(
    store: sqlite3.Connection,
) -> None:
    first = _fit("xg_form")
    second = _fit("rest")
    old_id = save_fit(store, FitRecord(first, 20242025, T0, label="live"))
    new_id = save_fit(store, FitRecord(second, 20252026, T0 + timedelta(days=1), label="live"))
    artifact = load_fit(store, FitRef(label="live"))
    assert artifact.fit_id == new_id != old_id
    assert artifact.fit == second


def test_an_unknown_label_raises_and_names_the_fix(store: sqlite3.Connection) -> None:
    save_fit(store, FitRecord(ELO_ONLY, 20252026, T0))
    with pytest.raises(StoreError, match="no fit carries the label 'live'.*--label live"):
        load_fit(store, FitRef(label="live"))


def test_an_unknown_fit_id_raises(store: sqlite3.Connection) -> None:
    save_fit(store, FitRecord(ELO_ONLY, 20252026, T0))
    with pytest.raises(UnknownFitError, match="fit 99"):
        load_fit(store, FitRef(fit_id=99))


def test_a_newer_label_free_fit_does_not_replace_the_labelled_one(
    store: sqlite3.Connection,
) -> None:
    live_id = save_fit(store, FitRecord(_fit("xg_form"), 20252026, T0, label="live"))
    save_fit(store, FitRecord(_fit("rest"), 20252026, T0 + timedelta(days=1)))
    assert load_fit(store, FitRef(label="live")).fit_id == live_id


def test_a_label_moves_and_keeps_every_earlier_row(store: sqlite3.Connection) -> None:
    first = save_fit(store, FitRecord(_fit("xg_form"), 20252026, T0))
    second = save_fit(store, FitRecord(ELO_ONLY, 20252026, T0 + timedelta(days=1)))
    label_fit(store, first, "live", T0)
    label_fit(store, second, "live", T0 + timedelta(days=1))
    assert load_fit(store, FitRef(fit_id=first)).labels == ("live",)
    rows = store.execute("SELECT fit_id, label FROM fit_labels ORDER BY rowid").fetchall()
    assert rows == [(first, "live"), (second, "live")]


def test_labelling_an_unknown_fit_raises(store: sqlite3.Connection) -> None:
    save_fit(store, FitRecord(ELO_ONLY, 20252026, T0))
    with pytest.raises(UnknownFitError, match="fit 99"):
        label_fit(store, 99, "live", T0)


@pytest.mark.parametrize("label", ["", " live "])
def test_a_stored_label_must_not_be_blank_or_padded(store: sqlite3.Connection, label: str) -> None:
    fit_id = save_fit(store, FitRecord(ELO_ONLY, 20252026, T0))
    with pytest.raises(ValueError, match="label"):
        label_fit(store, fit_id, label, T0)


def test_a_fit_saved_without_a_through_season_loads_with_none(
    store: sqlite3.Connection,
) -> None:
    store.execute(
        "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
        ' VALUES (NULL, \'[]\', \'{"intercept": 0, "elo_weight": 1, "features": {}}\','
        " '2026-01-01T00:00:00.000000+00:00')"
    )
    artifact = load_fit(store, FitRef(fit_id=1))
    assert artifact.fit_through_season is None


def test_load_an_empty_store_names_the_fix(store: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match="no fit is saved.*hockey-model fit --label live"):
        load_fit(store, FitRef(label="live"))
    with pytest.raises(UnknownFitError, match="fit 1"):
        load_fit(store, FitRef(fit_id=1))
