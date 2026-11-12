"""The backtest on a small made-up store: the wiring from the store to the report."""

import sqlite3

import pytest
from seeding import add_game, add_goalie, add_goalie_form, add_team_form

from hockey_model.backtest.runner import run_backtest
from hockey_model.game_model.features import ATTEMPT_FEATURES, FEATURES
from hockey_model.game_model.seasons import SeasonPlan
from hockey_model.types import HockeyModelError

PLAN = SeasonPlan(
    burn_in=1, fit_first=2, select_train=(2, 4), select_validate=(5, 5), scored=(6, 7)
)


GOALIE = {"TOR": 11, "BOS": 21}


def seed(
    store: sqlite3.Connection,
    seasons: range,
    *,
    short: frozenset[int] = frozenset(),
    goalie_form: bool = True,
) -> None:
    game_id = 0
    for season in seasons:
        # A short season has few games, so the first game of each team is a large share.
        for day in range(1, 6 if season in short else 21):
            game_id += 1
            score = (3, 2) if (game_id * 7) % 5 < 3 else (1, 2)
            home, away = ("TOR", "BOS") if day % 2 else ("BOS", "TOR")
            add_game(
                store,
                game_id,
                f"20{season + 10:02d}-11-{day:02d}T00:00",
                home=home,
                away=away,
                season=season,
                score=score,
            )
            # Form and a starting goalie for both teams in every game, so the features are
            # measured, not imputed.
            add_team_form(store, game_id, home, 2.6, 2.5)
            add_team_form(store, game_id, away, 2.5, 2.6)
            for team, nhl_id in ((home, GOALIE[home]), (away, GOALIE[away])):
                add_goalie(store, game_id, team, nhl_id, f"Goalie {team}")
                if goalie_form:
                    add_goalie_form(store, game_id, nhl_id, 2.5, 2)


def test_the_report_from_a_small_store(store: sqlite3.Connection) -> None:
    seed(store, range(1, 8))
    report, records = run_backtest(store, PLAN, candidates=FEATURES)
    assert report.candidate_features == ("xg_form", "goalie_gsax", "rest")
    assert [r.name for r in report.runs] == ["home_rate", "elo", "model_actual", "model_guess"]
    assert all(r.games == 40 for r in report.runs)
    assert len(records) == 3 * 40
    assert {r.game_id for r in records if r.run == "elo"} == set(range(101, 141))


def test_the_backtest_needs_games_in_the_scored_seasons(store: sqlite3.Connection) -> None:
    seed(store, range(1, 6))
    with pytest.raises(HockeyModelError, match="scored seasons"):
        run_backtest(store, PLAN, candidates=FEATURES)


def test_the_backtest_needs_games_in_the_selection_seasons(store: sqlite3.Connection) -> None:
    with pytest.raises(HockeyModelError, match="selection seasons"):
        run_backtest(store, PLAN, candidates=FEATURES)


def test_the_attempt_candidates_replace_xg_form(store: sqlite3.Connection) -> None:
    seed(store, range(1, 8))
    report, _ = run_backtest(store, PLAN, candidates=ATTEMPT_FEATURES)
    assert report.candidate_features == ("attempt_form", "goalie_gsax", "rest")
    assert set(report.validation) == {"elo", "elo+attempt_form", "elo+goalie_gsax", "elo+rest"}
    assert "xg_form" not in report.tested_features


def test_an_attempt_run_ships_nothing_and_says_so(store: sqlite3.Connection) -> None:
    seed(store, range(1, 8))
    report, _ = run_backtest(store, PLAN, candidates=ATTEMPT_FEATURES)
    assert report.live_features == ()
    assert any("Nothing ships from it" in note for note in report.notes)
    assert not any(kind.startswith("xg_form") for kind in report.missing_inputs)


def test_a_candidate_over_the_limit_only_in_a_walk_forward_window_is_vetoed(
    store: sqlite3.Connection,
) -> None:
    # Season 2 has five games, so the first game is a fifth of its rows. Selection trains on
    # seasons 2 to 4, where the share is small. The first scoring fit uses season 2 alone.
    plan = SeasonPlan(
        burn_in=2, fit_first=2, select_train=(2, 4), select_validate=(5, 5), scored=(3, 7)
    )
    seed(store, range(2, 8), short=frozenset({2}))
    report, _ = run_backtest(store, plan, candidates=FEATURES)
    assert {"xg_form", "rest"} <= set(report.vetoed)
    assert not {"xg_form", "rest"} & set(report.tested_features)


def test_a_candidate_imputed_in_every_training_row_is_vetoed_and_the_elo_is_unchanged(
    store: sqlite3.Connection,
) -> None:
    # No goalie has any MoneyPuck row, so goalie_gsax is imputed in every training row.
    seed(store, range(1, 8), goalie_form=False)
    report, _ = run_backtest(store, PLAN, candidates=FEATURES)
    assert "goalie_gsax" in report.vetoed
    assert "goalie_gsax" not in report.tested_features
    assert "elo+goalie_gsax" not in report.validation
    assert "goalie_gsax" not in report.live_features
    # The run goes on with the other candidates.
    assert {"elo+xg_form", "elo+rest"} <= set(report.validation)
    # The Elo run does not depend on which candidates were refused.
    elo = next(run for run in report.runs if run.name == "elo")
    only_rest, _ = run_backtest(store, PLAN, candidates=("rest",))
    assert next(run for run in only_rest.runs if run.name == "elo") == elo
