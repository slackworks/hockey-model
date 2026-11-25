"""A change to a feature formula needs a new SPEC_REVISION (#346)."""

import hashlib
import json
import sqlite3

from seeding import add_game, add_goalie, add_goalie_form, add_team_form, utc

from hockey_model.game import Game
from hockey_model.game_model.elo import EloBook
from hockey_model.game_model.features import FEATURES, GoalieMode, build_features
from hockey_model.model_version import SPEC_REVISION

# The digest of the feature values below, for each SPEC_REVISION. A formula change moves the
# digest. The author then bumps SPEC_REVISION and adds the new digest under the new revision.
FEATURE_DIGESTS = {1: "afe8b85438fd8d81"}

HOME, AWAY = "TOR", "BOS"
AS_OF = utc("2026-01-19T16:00")
DIGITS = 9


def _seed(store: sqlite3.Connection) -> None:
    """Three days of history for both teams, each game with MoneyPuck form and a goalie."""
    for day in (17, 18, 19):
        gid = 10 + day
        add_game(store, gid, f"2026-01-{day:02d}T00:00", home=HOME, away="MTL")
        add_game(store, gid + 100, f"2026-01-{day:02d}T00:00", home="MTL", away=AWAY)
        add_team_form(store, gid, HOME, 3.0 + day / 100, 1.0)
        add_team_form(store, gid + 100, AWAY, 1.0, 3.0 - day / 100)
        add_goalie(store, gid, HOME, 11, "C. Ingram")
        add_goalie(store, gid + 100, AWAY, 21, "J. Swayman")
        add_goalie_form(store, gid, 11, 3.0 + day / 50, 1)
        add_goalie_form(store, gid + 100, 21, 2.0, 3)


def _digest(store: sqlite3.Connection) -> str:
    game = Game(100, HOME, AWAY, utc("2026-01-20T00:00"))
    row = build_features(
        store, game, AS_OF, EloBook(), goalie_mode=GoalieMode.GUESS, names=FEATURES
    )
    values = {name: round(row.values[name], DIGITS) for name in sorted(row.values)}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def test_the_feature_values_match_the_pin_of_the_spec_revision(store: sqlite3.Connection) -> None:
    _seed(store)
    digest = _digest(store)
    assert FEATURE_DIGESTS.get(SPEC_REVISION) == digest, (
        f"The feature values changed. Bump SPEC_REVISION in model_version.py, then set "
        f"FEATURE_DIGESTS[{SPEC_REVISION + 1}] = {digest!r} in this file."
    )
