"""Tests for fit selection by identity or label, and the provenance the loader returns (#80)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from hockey_model.consumer_errors import UnknownFitError
from hockey_model.game_model.logistic import ELO_ONLY, LogisticFit
from hockey_model.store.db import StoreError
from hockey_model.store.fits import FitArtifact, FitRecord, FitRef, load_fit, save_fit

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


def test_a_ref_refuses_a_padded_label_as_a_saved_label_does() -> None:
    # A saved label with a space cannot be typed back as a ref, so the ref refuses it early.
    with pytest.raises(ValueError, match="must not carry spaces around it"):
        FitRef(label=" live ")


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
    first = save_fit(store, FitRecord(_fit("xg_form"), 20252026, T0, label="live"))
    second = save_fit(store, FitRecord(ELO_ONLY, 20252026, T0 + timedelta(days=1), label="live"))
    assert load_fit(store, FitRef(fit_id=first)).labels == ()
    assert load_fit(store, FitRef(fit_id=second)).labels == ("live",)
    assert load_fit(store, FitRef(label="live")).fit_id == second
    rows = store.execute("SELECT fit_id, label FROM fit_labels ORDER BY rowid").fetchall()
    assert rows == [(first, "live"), (second, "live")]


def test_one_fit_carries_several_labels(store: sqlite3.Connection) -> None:
    fit_id = save_fit(store, FitRecord(ELO_ONLY, 20252026, T0, label="live"))
    store.execute(
        "INSERT INTO fit_labels (label, fit_id, set_at) VALUES (?,?,?)",
        ("reviewed", fit_id, "2026-01-02T00:00:00.000000+00:00"),
    )
    assert load_fit(store, FitRef(fit_id=fit_id)).labels == ("live", "reviewed")


@pytest.mark.parametrize("label", ["", " live "])
def test_a_stored_label_must_not_be_blank_or_padded(store: sqlite3.Connection, label: str) -> None:
    with pytest.raises(ValueError, match="label"):
        save_fit(store, FitRecord(ELO_ONLY, 20252026, T0, label=label))
    assert store.execute("SELECT COUNT(*) FROM model_fits").fetchone() == (0,)


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


def test_a_fit_round_trips_its_game_cutoff(store: sqlite3.Connection) -> None:
    """The game cutoff is stored beside the weights, so no column and no migration is needed."""
    read_through = T0 + timedelta(days=3)
    fit_id = save_fit(store, FitRecord(ELO_ONLY, 20252026, T0, trained_through=read_through))
    assert load_fit(store, FitRef(fit_id=fit_id)).trained_through == read_through


def test_a_fit_saved_without_a_game_cutoff_loads_with_none(store: sqlite3.Connection) -> None:
    fit_id = save_fit(store, FitRecord(ELO_ONLY, 20252026, T0))
    assert load_fit(store, FitRef(fit_id=fit_id)).trained_through is None


def test_a_malformed_game_cutoff_names_the_fix(store: sqlite3.Connection) -> None:
    store.execute(
        "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
        ' VALUES (20252026, \'[]\', \'{"intercept": 0, "elo_weight": 1, "features": {},'
        ' "trained_through": "last Tuesday"}\', \'2026-01-01T00:00:00.000000+00:00\')'
    )
    with pytest.raises(StoreError, match="game cutoff 'last Tuesday'.*hockey-model fit"):
        load_fit(store, FitRef(fit_id=1))


def test_a_fit_with_no_cutoff_season_covers_no_game(store: sqlite3.Connection) -> None:
    assert FitArtifact(1, ELO_ONLY, None, T0).covers_game(20252026, T0) is None


def test_a_game_after_the_stored_game_cutoff_is_not_covered(store: sqlite3.Connection) -> None:
    artifact = FitArtifact(1, ELO_ONLY, 20252026, T0, trained_through=T0)
    assert artifact.covers_game(20252026, T0 - timedelta(days=1)) is True
    assert artifact.covers_game(20252026, T0 + timedelta(days=1)) is False


def test_a_game_before_the_first_fit_season_is_not_covered(store: sqlite3.Connection) -> None:
    """The Elo burn-in season replays results, but no fit trains on one of its feature rows."""
    artifact = FitArtifact(1, ELO_ONLY, 20252026, T0, trained_through=T0)
    assert artifact.covers_game(20142015, T0 - timedelta(days=1)) is False


def test_a_fit_with_no_game_cutoff_is_bounded_by_its_save_time(
    store: sqlite3.Connection,
) -> None:
    """An older fit read no game that starts after its save, so ``created_at`` is the bound."""
    artifact = FitArtifact(1, ELO_ONLY, 20252026, T0)
    assert artifact.covers_game(20252026, T0 - timedelta(days=1)) is True
    assert artifact.covers_game(20252026, T0 + timedelta(days=1)) is False


def test_load_an_empty_store_names_the_fix(store: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match="no fit is saved.*hockey-model fit --label live"):
        load_fit(store, FitRef(label="live"))
    with pytest.raises(UnknownFitError, match="fit 1"):
        load_fit(store, FitRef(fit_id=1))
