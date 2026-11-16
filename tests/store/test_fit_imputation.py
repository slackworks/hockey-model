"""The imputation quality of a fit is stored with the fit, and only a fit that passed is saved."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.game_model.logistic import LogisticFit
from hockey_model.store.db import StoreError
from hockey_model.store.fits import FitRecord, save_fit
from hockey_model.types import HockeyModelError

T0 = datetime(2026, 11, 1, tzinfo=UTC)
FIT = LogisticFit(("rest",), 0.0, 1.0, {"rest": 0.5})
QUALITY = [{"feature": "rest", "imputed": 1, "total": 10, "share": 0.1}]


def _stored_imputation(store: sqlite3.Connection, fit_id: int) -> object:
    (raw,) = store.execute("SELECT weights FROM model_fits WHERE fit_id = ?", (fit_id,)).fetchone()
    return json.loads(raw)["imputation"]


def test_the_quality_is_stored_with_the_fit(store: sqlite3.Connection) -> None:
    fit_id = save_fit(store, FitRecord(FIT, 20252026, T0, QUALITY))
    assert _stored_imputation(store, fit_id) == QUALITY


def test_a_fit_with_no_features_stores_an_empty_list(store: sqlite3.Connection) -> None:
    fit_id = save_fit(store, FitRecord(FIT, 20252026, T0))
    assert _stored_imputation(store, fit_id) == []


@given(
    st.lists(
        st.fixed_dictionaries(
            {
                "feature": st.sampled_from(["xg_form", "rest", "goalie_gsax"]),
                "imputed": st.integers(0, 50),
                "total": st.integers(1, 50),
                "share": st.floats(0.0, 1.0, allow_nan=False),
            }
        ),
        max_size=3,
    )
)
def test_any_quality_list_round_trips_unchanged(entries: list[dict[str, object]]) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = None
    conn.execute(
        "CREATE TABLE model_fits (fit_id INTEGER PRIMARY KEY, fit_through_season INTEGER,"
        " features TEXT, weights TEXT, created_at TEXT)"
    )
    fit_id = save_fit(conn, FitRecord(FIT, 20252026, T0, entries))
    assert _stored_imputation(conn, fit_id) == entries
    conn.close()


def test_an_entry_without_its_keys_raises_and_saves_no_row(store: sqlite3.Connection) -> None:
    bad = [{"feature": "rest", "imputed": 1}]
    with pytest.raises(StoreError, match="imputation entry"):
        save_fit(store, FitRecord(FIT, 20252026, T0, bad))
    assert store.execute("SELECT COUNT(*) FROM model_fits").fetchone()[0] == 0


def test_a_fit_with_a_bad_weight_still_saves_nothing(store: sqlite3.Connection) -> None:
    bad = LogisticFit(("rest",), 0.0, 1.0, {"rest": float("nan")})
    with pytest.raises(HockeyModelError):
        save_fit(store, FitRecord(bad, 20252026, T0, QUALITY))
    assert store.execute("SELECT COUNT(*) FROM model_fits").fetchone()[0] == 0
