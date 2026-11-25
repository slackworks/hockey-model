"""A change to a feature formula needs a new SPEC_REVISION (#346)."""

import sqlite3

import pytest
from seeding import (
    add_game,
    add_goalie,
    add_goalie_form,
    add_starter_read,
    add_team_form,
    utc,
)

from hockey_model.game import Game
from hockey_model.game_model.elo import EloBook
from hockey_model.game_model.features import FEATURES, FeatureRow, GoalieMode, build_features
from hockey_model.json_hash import json_sha256
from hockey_model.model_version import SPEC_REVISION

# The digest of the feature rows below, for each SPEC_REVISION. A formula change moves the digest.
# The author then bumps SPEC_REVISION and adds the new digest under the new revision.
FEATURE_DIGESTS = {1: "c7091120d37f2d76"}

HOME, AWAY = "TOR", "BOS"
START = utc("2026-01-20T00:00")
AS_OF = utc("2026-01-19T16:00")
# Rounded so that a libm difference between two machines cannot move the digest.
DIGITS = 6
DIGEST_LENGTH = 16


def _seed(store: sqlite3.Connection) -> EloBook:
    """Seed the history of both teams, and return an Elo book that has played its games.

    TOR played the day before the game and BOS two days before, so the rest feature is not zero.
    """
    book = EloBook()
    for day in (17, 18, 19):
        gid = 10 + day
        stamp = f"2026-01-{day:02d}T00:00"
        book.update(add_game(store, gid, stamp, home=HOME, away="MTL"))
        add_team_form(store, gid, HOME, 3.0 + day / 100, 1.0)
        add_goalie(store, gid, HOME, 11, "C. Ingram")
        add_goalie_form(store, gid, 11, 3.0 + day / 50, 1)
        if day != 19:
            book.update(add_game(store, gid + 100, stamp, home="MTL", away=AWAY, score=(1, 4)))
            add_team_form(store, gid + 100, AWAY, 1.0, 3.0 - day / 100)
            add_goalie(store, gid + 100, AWAY, 21, "J. Swayman")
            add_goalie_form(store, gid + 100, 21, 2.0, 3)
    # An earlier start of the confirmed goalie, the boxscore goalies of game 100, and a read.
    add_game(store, 50, "2026-01-15T00:00", home=HOME, away="MTL")
    add_goalie(store, 50, HOME, 12, "J. Woll")
    add_goalie_form(store, 50, 12, 0.5, 4)
    add_game(store, 100, "2026-01-20T00:00", state="FUT")
    add_goalie(store, 100, HOME, 12, "J. Woll")
    add_goalie(store, 100, AWAY, 21, "J. Swayman")
    add_starter_read(
        store, 100, HOME, "espn", "Joseph Woll", "confirmed", fetched_at="2026-01-19T14:00"
    )
    return book


def _summary(row: FeatureRow) -> dict[str, object]:
    return {
        "values": {name: round(row.values[name], DIGITS) for name in sorted(row.values)},
        "elo_logit": round(row.elo_logit, DIGITS),
        "missing": sorted(f"{gap.kind}:{gap.team}" for gap in row.missing),
    }


def _digest(store: sqlite3.Connection, book: EloBook) -> str:
    game = Game(100, HOME, AWAY, START)
    summary: dict[str, object] = {}
    for mode, as_of in (
        (GoalieMode.GUESS, AS_OF),
        (GoalieMode.READS, AS_OF),
        (GoalieMode.ACTUAL, START),
    ):
        row = build_features(store, game, as_of, book, goalie_mode=mode, names=FEATURES)
        assert row.imputed == (), f"{mode}: a seeded feature fell back to a neutral value"
        summary[str(mode)] = _summary(row)
    return json_sha256(summary)[:DIGEST_LENGTH]


def test_the_seed_makes_every_feature_move(store: sqlite3.Connection) -> None:
    book = _seed(store)
    game = Game(100, HOME, AWAY, START)
    row = build_features(store, game, AS_OF, book, goalie_mode=GoalieMode.READS, names=FEATURES)
    assert all(row.values[name] != 0.0 for name in FEATURES)
    assert row.elo_logit != 0.0


def test_the_feature_rows_match_the_pin_of_the_spec_revision(store: sqlite3.Connection) -> None:
    digest = _digest(store, _seed(store))
    if SPEC_REVISION not in FEATURE_DIGESTS:
        pytest.fail(
            f"SPEC_REVISION is {SPEC_REVISION} and has no pin. "
            f"Add FEATURE_DIGESTS[{SPEC_REVISION}] = {digest!r} to this file."
        )
    assert FEATURE_DIGESTS[SPEC_REVISION] == digest, (
        f"The feature rows changed with SPEC_REVISION still at {SPEC_REVISION}. Bump "
        f"SPEC_REVISION in model_version.py, then add FEATURE_DIGESTS[{SPEC_REVISION + 1}] = "
        f"{digest!r} to this file."
    )
