"""The version of the model specification that a report names (#331).

A consumer approves a model by its version, so the version changes only when the specification
changes. It covers the fit features and the constants that define the features and the Elo
ratings. It leaves out the fit weights, so a routine refit keeps the version.

``SPEC_REVISION`` is for changes that the feature list cannot name: a change to a feature formula
or to the model form. Bump it in the same change.
``tests/game_model/test_feature_pin.py`` fails when a feature value changes with no bump.
"""

from collections.abc import Sequence

from hockey_model.game_model.elo import ELO_HOME_ADV, ELO_INITIAL, ELO_K
from hockey_model.game_model.features import FORM_WINDOW, GOALIE_WINDOW, REST_CAP_DAYS
from hockey_model.json_hash import json_sha256

SPEC_REVISION = 1
# Hex digits of the SHA-256 that names a version. Sixteen digits make a collision unlikely.
_DIGEST_LENGTH = 16


def model_version(features: Sequence[str]) -> str:
    """Return the version of a model that fits ``features`` with the current specification.

    The feature order does not matter, so the same set of features always gives one version.
    """
    spec = {
        "revision": SPEC_REVISION,
        "features": sorted(features),
        "elo": [ELO_INITIAL, ELO_K, ELO_HOME_ADV],
        "form_window": FORM_WINDOW,
        "goalie_window": GOALIE_WINDOW,
        "rest_cap_days": REST_CAP_DAYS,
    }
    return json_sha256(spec)[:_DIGEST_LENGTH]
