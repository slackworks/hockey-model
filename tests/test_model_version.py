"""The model version changes with the specification and never with a refit (#331)."""

from dataclasses import replace

from hockey_model.daily_report import model_identity
from hockey_model.game_model.features import FEATURES
from hockey_model.game_model.logistic import ELO_ONLY, LogisticFit
from hockey_model.model_version import model_version

# A change detector. A new value means the specification changed: bump ``SPEC_REVISION`` or
# change the features, then update this value in the same change.
DEFAULT_FEATURES_VERSION = "91e2726931ac49f6"


def _fit(features: tuple[str, ...], weight: float) -> LogisticFit:
    return replace(
        ELO_ONLY,
        features=features,
        weights={name: weight for name in features},
    )


def test_the_default_features_have_the_pinned_version() -> None:
    assert model_version(FEATURES) == DEFAULT_FEATURES_VERSION


def test_the_feature_order_does_not_change_the_version() -> None:
    assert model_version(("rest", "xg_form")) == model_version(("xg_form", "rest"))


def test_a_refit_with_new_weights_keeps_the_version() -> None:
    first = model_identity(_fit(("xg_form", "rest"), 0.1), None)
    refit = model_identity(_fit(("xg_form", "rest"), 0.2), None)
    assert first.fit_sha256 != refit.fit_sha256
    assert first.version == refit.version


def test_a_promoted_feature_changes_the_version() -> None:
    before = model_identity(_fit(("xg_form",), 0.1), None)
    after = model_identity(_fit(("xg_form", "rest"), 0.1), None)
    assert before.version != after.version


def test_a_code_change_keeps_the_version() -> None:
    assert model_identity(_fit(("rest",), 0.1), "aaa").version == (
        model_identity(_fit(("rest",), 0.1), "bbb").version
    )


def test_the_version_is_sixteen_lowercase_hex_digits() -> None:
    version = model_identity(_fit(("rest",), 0.1), None).version
    assert version is not None
    assert len(version) == 16
    assert set(version) <= set("0123456789abcdef")
