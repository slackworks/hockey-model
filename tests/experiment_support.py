"""Shared helpers for the experiment checker tests."""

from datetime import datetime, timedelta
from pathlib import Path

from seeding import add_game, add_goalie, add_goalie_form, add_team_form

from hockey_model.backtest.metrics import MIN_BLOCK_LENGTH, MIN_INDEPENDENT_BLOCKS
from hockey_model.experiments.models import ExperimentRecord

VALID_RECORD_DATA = {
    "id": "0001-valid",
    "title": "Valid record",
    "hypothesis": "Feature X improves log loss.",
    "cohort": "confirmation",
    "data_cutoff": "2026-10-31",
    "primary_metric": "mean_per_game_log_loss",
    "baseline": {"fit_id": 1, "commit": "abc123"},
    "selection_folds": [[20152016, 20212022]],
    "min_games": 10,
    "candidate_features": ["xg_form", "rest"],
    "look_number": 1,
    "expected_effect": None,
    "promotion_criterion": "95th percentile below zero",
    "failure_outcome": "ship Elo alone",
    "status": "registered",
}

# Seasons a confirmation run needs: selection seasons, validation seasons, and
# seasons to fit on before the scored cohort.
FIT_SEASONS = range(20152016, 20262027, 10001)

# The scored cohort of a run needs this many games for a bootstrap interval.
ENOUGH_GAMES = MIN_INDEPENDENT_BLOCKS * MIN_BLOCK_LENGTH


def make_record(data: dict | None = None) -> ExperimentRecord:
    """Create a valid record from base data, overridden by data."""
    merged = {**VALID_RECORD_DATA, **(data or {})}
    return ExperimentRecord.from_dict(merged)


def register(registry: Path, data: dict | None = None) -> ExperimentRecord:
    """Write a record into the fixture registry and return it."""
    record = make_record(data)
    record.save(registry / f"{record.id}.json")
    return record


def seed(store, seasons, *, game_type: int = 2, first_id: int = 1, games: int = 20) -> int:
    """Put ``games`` final games in each season and return the last game id used."""
    game_id = first_id - 1
    for season in seasons:
        for index in range(games):
            game_id += 1
            start = datetime(season // 10000, 11, 1) + timedelta(
                days=index % 20, minutes=(index // 20) * 90
            )
            add_game(
                store, game_id, start.strftime("%Y-%m-%dT%H:%M"), season=season, game_type=game_type
            )
            # Form and a starter for both teams, so the features are measured, not imputed.
            add_team_form(store, game_id, "TOR", 2.6, 2.5)
            add_team_form(store, game_id, "BOS", 2.5, 2.6)
            add_goalie(store, game_id, "TOR", 11, "G TOR")
            add_goalie(store, game_id, "BOS", 21, "G BOS")
            add_goalie_form(store, game_id, 11, 2.5, 2)
            add_goalie_form(store, game_id, 21, 2.5, 2)
    return game_id
