"""Tests for the backtest runner. They feed it made-up feature rows, so they run fast."""

import csv
import json
import math
import random
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hockey_model.backtest import eligibility, runner
from hockey_model.backtest.game_records import GameRecord, write_game_records
from hockey_model.backtest.runner import MONEYPUCK_LEAK_NOTE, run_backtest
from hockey_model.game import MissingInput, MissingKind
from hockey_model.game_model.features import FEATURES, FeatureRow, GoalieMode
from hockey_model.game_model.seasons import SeasonPlan
from hockey_model.game_model.training import TrainingRow
from hockey_model.sources.moneypuck import MONEYPUCK_CREDIT
from hockey_model.sources.nhl import GameRow
from hockey_model.store.timestamps import parse_iso_utc
from hockey_model.types import HockeyModelError

PLAN = SeasonPlan(
    burn_in=1, fit_first=2, select_train=(2, 4), select_validate=(5, 5), scored=(6, 7)
)
GAMES_PER_SEASON = 400


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z))


def dataset(
    *, xg_effect: float = 0.0, goalie_effect: float = 0.0, guess: str = "same", seed: int = 3
) -> dict[GoalieMode, list[TrainingRow]]:
    """Seven seasons of games. The home win chance depends on the Elo logit and the effects."""
    rng = random.Random(seed)
    actual: list[TrainingRow] = []
    guessed: list[TrainingRow] = []
    start = datetime(2015, 10, 1, tzinfo=UTC)
    game_id = 0
    for season in range(1, 8):
        for _ in range(GAMES_PER_SEASON):
            game_id += 1
            elo = rng.gauss(0.1, 0.3)
            xg = rng.gauss(0.0, 0.15)
            goalie = rng.gauss(0.0, 0.8)
            won = rng.random() < sigmoid(elo + xg_effect * xg + goalie_effect * goalie)
            row = GameRow(
                game_id,
                season,
                2,
                start + timedelta(hours=game_id),
                "TOR",
                "BOS",
                "OFF",
                3 if won else 2,
                2 if won else 3,
                "REG",
            )
            values = {"xg_form": xg, "goalie_gsax": goalie, "rest": 0.0}
            actual.append(TrainingRow(row, FeatureRow(game_id, elo, values, (), True), won))
            wrong = {"same": goalie, "opposite": -goalie, "partial": goalie + rng.gauss(0.0, 0.2)}[
                guess
            ]
            gvalues = {**values, "goalie_gsax": wrong}
            guessed.append(TrainingRow(row, FeatureRow(game_id, elo, gvalues, (), True), won))
    return {GoalieMode.ACTUAL: actual, GoalieMode.GUESS: guessed}


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch, store: sqlite3.Connection):
    """Return a function that makes the runner read the given dataset. It returns the store."""

    def use(rows: dict[GoalieMode, list[TrainingRow]]) -> sqlite3.Connection:
        def fake(conn, first, last, mode, burn_in=0, names=FEATURES):
            del conn, burn_in, names
            return [r for r in rows[mode] if first <= r.game.season <= last]

        monkeypatch.setattr(runner, "training_rows", fake)
        return store

    return use


def run(rows: dict[GoalieMode, list[TrainingRow]], feed):
    return run_backtest(feed(rows), PLAN, candidates=FEATURES)


def named(report, name: str):
    return next(r for r in report.runs if r.name == name)


def test_useful_features_pass_the_joint_test(feed) -> None:
    report, _ = run(dataset(xg_effect=6.0, goalie_effect=0.8), feed)
    assert report.success
    assert report.vetoed == ()
    assert report.tested_features == FEATURES
    assert report.live_features == report.tested_features
    assert named(report, "model_actual").diff_vs_elo_ci[1] < 0


def test_failed_joint_test_ships_elo_only(feed) -> None:
    report, _ = run(dataset(), feed)
    assert not report.success
    assert report.live_features == ()
    assert named(report, "model_actual").diff_vs_elo_ci[1] >= 0
    assert "the Elo alone ships" in " ".join(report.notes)


def test_success_is_decided_by_the_actual_run(feed) -> None:
    for effect in (0.0, 6.0):
        report, _ = run(dataset(xg_effect=effect), feed)
        model = named(report, "model_actual")
        assert report.success == (model.diff_vs_elo_ci[1] < 0)


def test_a_goalie_feature_that_hurts_the_guess_run_is_vetoed(feed) -> None:
    report, _ = run(dataset(goalie_effect=1.0, guess="opposite"), feed)
    assert "goalie_gsax" in report.vetoed
    assert "goalie_gsax" not in report.tested_features
    guess = report.validation["elo+goalie_gsax"]
    assert guess["guess"] > report.validation["elo"]["guess"]
    assert guess["actual"] < report.validation["elo"]["actual"]


def test_actual_mode_does_not_drop_a_feature(feed) -> None:
    report, _ = run(dataset(goalie_effect=1.0, guess="opposite"), feed)
    assert report.validation["elo+goalie_gsax"]["actual"] < report.validation["elo"]["actual"]
    assert (
        "xg_form" not in report.vetoed
        or report.validation["elo+xg_form"]["guess"] > (report.validation["elo"]["guess"])
    )


def test_a_feature_with_no_variation_is_not_vetoed(feed) -> None:
    report, _ = run(dataset(), feed)
    assert "rest" not in report.vetoed
    assert "rest" in report.tested_features
    assert report.validation["elo+rest"]["guess"] == pytest.approx(
        report.validation["elo"]["guess"]
    )


def test_validation_lists_every_candidate(feed) -> None:
    report, _ = run(dataset(), feed)
    assert set(report.validation) == {"elo", *[f"elo+{f}" for f in FEATURES]}
    assert all(set(v) == {"actual", "guess"} for v in report.validation.values())
    assert report.validation["elo"]["actual"] == report.validation["elo"]["guess"]
    assert report.candidate_features == FEATURES


def test_the_fit_is_redone_for_each_scored_season(feed, monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    original = runner.fit_logistic

    def spy(rows, wins, features):
        sizes.append(len(rows))
        return original(rows, wins, features)

    monkeypatch.setattr(runner, "fit_logistic", spy)
    run(dataset(), feed)
    first, second = sizes[-2:]
    assert first == 4 * GAMES_PER_SEASON
    assert second == 5 * GAMES_PER_SEASON


def test_the_scored_games_are_the_scored_seasons_only(feed) -> None:
    report, records = run(dataset(), feed)
    assert named(report, "model_actual").games == 2 * GAMES_PER_SEASON
    assert len(records) == 3 * 2 * GAMES_PER_SEASON
    assert {r.run for r in records} == {"elo", "model_actual", "model_guess"}


def test_the_runs_and_their_intervals(feed) -> None:
    report, _ = run(dataset(), feed)
    assert [r.name for r in report.runs] == ["home_rate", "elo", "model_actual", "model_guess"]
    assert named(report, "elo").diff_vs_elo_ci is None
    for name in ("home_rate", "model_actual", "model_guess"):
        low, high = named(report, name).diff_vs_elo_ci
        assert low <= high
    assert all(len(r.calibration) == 10 for r in report.runs)


def test_the_elo_run_is_the_raw_elo(feed) -> None:
    rows = dataset()
    _, records = run(rows, feed)
    by_id = {r.features.game_id: r for r in rows[GoalieMode.ACTUAL]}
    for record in [r for r in records if r.run == "elo"][:20]:
        assert record.home_win_prob == pytest.approx(
            sigmoid(by_id[record.game_id].features.elo_logit)
        )


def test_the_model_guess_run_uses_the_actual_fit_on_guess_features(feed) -> None:
    _, records = run(dataset(goalie_effect=1.0, guess="partial"), feed)
    actual = {r.game_id: r.home_win_prob for r in records if r.run == "model_actual"}
    guess = {r.game_id: r.home_win_prob for r in records if r.run == "model_guess"}
    assert any(abs(actual[g] - guess[g]) > 1e-6 for g in actual)


def test_moneypuck_note_and_credit_when_a_moneypuck_feature_is_tested(feed) -> None:
    report, _ = run(dataset(xg_effect=6.0), feed)
    assert MONEYPUCK_LEAK_NOTE in report.notes
    assert report.credits == (MONEYPUCK_CREDIT,)
    assert "xg_form" in report.tested_features


def test_no_moneypuck_note_when_only_rest_is_tested(monkeypatch: pytest.MonkeyPatch) -> None:
    assert MONEYPUCK_LEAK_NOTE not in runner._notes(("rest",), (), success=True)
    assert MONEYPUCK_LEAK_NOTE in runner._notes(("rest", "xg_form"), (), success=True)


def test_the_notes_state_how_many_features_were_tested(feed) -> None:
    report, _ = run(dataset(), feed)
    assert "3 candidate features" in report.notes[0]


def test_no_games_in_the_selection_seasons_is_an_error(feed) -> None:
    rows = dataset()
    short = {mode: [r for r in rs if r.game.season <= 3] for mode, rs in rows.items()}
    with pytest.raises(HockeyModelError, match="no games in the selection seasons"):
        run(short, feed)


def test_no_scored_games_is_an_error(feed) -> None:
    rows = dataset()
    short = {mode: [r for r in rs if r.game.season <= 5] for mode, rs in rows.items()}
    with pytest.raises(HockeyModelError, match="scored seasons"):
        run(short, feed)


def test_game_records_csv(feed, tmp_path: Path) -> None:
    _, records = run(dataset(), feed)
    target = tmp_path / "games.csv"
    write_game_records(target, records)
    with target.open(newline="") as handle:
        table = list(csv.DictReader(handle))
    assert list(table[0]) == [
        "game_id",
        "start",
        "home",
        "away",
        "run",
        "home_win_prob",
        "home_won",
    ]
    assert len(table) == len(records)
    assert table[0]["start"].endswith("+00:00")
    assert {row["home_won"] for row in table} <= {"True", "False"}
    assert not any("price" in name or "odds" in name for name in table[0])


def test_game_record_fields() -> None:
    record = GameRecord(1, datetime(2026, 1, 1, tzinfo=UTC), "TOR", "BOS", "elo", 0.5, True)
    assert record.run == "elo"


def test_the_report_counts_scored_games_with_missing_inputs(feed) -> None:
    rows = dataset()
    note = MissingInput(MissingKind.XG_FORM, "TOR", "no MoneyPuck games")
    scored = [r for r in rows[GoalieMode.ACTUAL] if r.game.season == 6][:10]
    for row in scored:
        object.__setattr__(row, "features", replace(row.features, missing=(note, note)))
    report, _ = run(rows, feed)
    assert report.missing_inputs == {"xg_form": 10}


def test_the_report_has_no_missing_counts_when_nothing_is_missing(feed) -> None:
    report, _ = run(dataset(), feed)
    assert report.missing_inputs == {}


def test_the_notes_say_the_guess_run_has_no_injury_history(feed) -> None:
    report, _ = run(dataset(), feed)
    assert runner.INJURY_HISTORY_NOTE in report.notes


_RECORDS = st.builds(
    GameRecord,
    game_id=st.integers(1, 10**10),
    start=st.datetimes(
        min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
    ),
    home=st.sampled_from(["TOR", "BOS", "UTA"]),
    away=st.sampled_from(["NYR", "EDM", "SEA"]),
    run=st.sampled_from(["elo", "model_actual", "model_guess"]),
    home_win_prob=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    home_won=st.booleans(),
)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(records=st.lists(_RECORDS, min_size=1, max_size=8))
def test_the_game_csv_reads_back_to_the_records(records: list[GameRecord], tmp_path: Path) -> None:
    target = tmp_path / "games.csv"
    write_game_records(target, records)
    with target.open(newline="") as handle:
        table = list(csv.DictReader(handle))
    back = [
        GameRecord(
            int(row["game_id"]),
            parse_iso_utc(row["start"]),
            row["home"],
            row["away"],
            row["run"],
            float(row["home_win_prob"]),
            row["home_won"] == "True",
        )
        for row in table
    ]
    assert back == records


def test_the_notes_say_why_the_two_model_runs_are_equal_without_a_goalie_feature() -> None:
    notes = runner._notes(("rest", "xg_form"), ("goalie_gsax",), success=True)
    assert runner.GUESS_EQUALS_ACTUAL_NOTE in notes


def test_the_notes_leave_out_that_remark_when_a_goalie_feature_is_tested() -> None:
    notes = runner._notes(("goalie_gsax", "rest"), (), success=True)
    assert runner.GUESS_EQUALS_ACTUAL_NOTE not in notes


def _thin(rows: dict[GoalieMode, list[TrainingRow]], per_season: int):
    """Keep only ``per_season`` games in each scored season."""
    kept: dict[GoalieMode, list[TrainingRow]] = {}
    for mode, source in rows.items():
        seen: dict[int, int] = {}
        kept[mode] = []
        for row in source:
            season = row.game.season
            seen[season] = seen.get(season, 0) + 1
            if season < PLAN.scored[0] or seen[season] <= per_season:
                kept[mode].append(row)
    return kept


def test_a_full_cohort_is_eligible_and_lists_its_counts(feed) -> None:
    report, _ = run(dataset(xg_effect=6.0, goalie_effect=0.8), feed)
    assert report.eligible
    assert report.ineligible_reasons == ()
    assert [(c.season, c.game_type, c.scored) for c in report.coverage] == [
        (6, 2, GAMES_PER_SEASON),
        (7, 2, GAMES_PER_SEASON),
    ]


def test_too_few_scored_games_refuse_success_but_keep_the_scores(feed) -> None:
    # The same strong effect passes in the full dataset above.
    rows = _thin(dataset(xg_effect=6.0, goalie_effect=0.8), 30)
    report, records = run(rows, feed)
    assert not report.eligible
    assert not report.success
    assert report.live_features == ()
    assert "independent blocks" in " ".join(report.ineligible_reasons)
    assert "Not eligible for success" in " ".join(report.notes)
    model = named(report, "model_actual")
    assert model.games == 60
    assert model.diff_vs_elo_ci is None
    assert model.mean_diff_vs_elo is not None
    assert model.log_loss > 0
    assert records


def test_a_partial_regular_season_refuses_success(feed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eligibility, "EXPECTED_REGULAR_GAMES", {6: GAMES_PER_SEASON, 7: 1000})
    report, _ = run(dataset(xg_effect=6.0, goalie_effect=0.8), feed)
    assert not report.success
    assert report.live_features == ()
    (reason,) = report.ineligible_reasons
    assert f"season 7 has {GAMES_PER_SEASON} scored regular season games" in reason
    seven = next(c for c in report.coverage if c.season == 7)
    assert seven.expected == 1000


def test_a_missing_scored_season_is_listed_with_zero_games(
    feed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eligibility, "EXPECTED_REGULAR_GAMES", {6: GAMES_PER_SEASON, 7: 400})
    rows = dataset()
    rows = {mode: [r for r in source if r.game.season != 7] for mode, source in rows.items()}
    report, _ = run(rows, feed)
    assert not report.success
    assert any(c.season == 7 and c.scored == 0 for c in report.coverage)


def test_the_json_carries_the_counts_and_the_reasons(feed) -> None:
    report, _ = run(_thin(dataset(), 30), feed)
    payload = json.loads(json.dumps(asdict(report)))
    assert payload["eligible"] is False
    assert payload["ineligible_reasons"]
    assert payload["coverage"][0] == {
        "season": 6,
        "game_type": 2,
        "scored": 30,
        "expected": None,
        "unresolved": 0,
    }
    assert payload["live_features"] == []
