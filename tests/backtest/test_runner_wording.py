"""Tests that pin the wording, the boundaries, and the wiring of the backtest runner (#288).

Each test names a rule that a mutation run showed no test held: a note text, the veto and
success boundaries, the credit rule, and the fields of the report and the records.
"""

import dataclasses
import re
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from test_runner_store import PLAN, seed

from hockey_model.backtest import runner
from hockey_model.backtest.eligibility import SeasonCount
from hockey_model.game_model.features import FEATURES
from hockey_model.types import HockeyModelError

REFRESH = "run `hockey-model refresh --backfill-from 2014-10-01`"


def test_the_notes_name_the_tested_count_the_vetoes_and_a_failed_test() -> None:
    notes = runner._notes(("rest",), ("xg_form", "goalie_gsax"), success=False)
    assert notes[0] == "3 candidate features were tested together as one joint test."
    assert "The guess run vetoed: xg_form, goalie_gsax." in notes
    assert "The joint test failed, so the Elo alone ships." in notes


def test_a_successful_joint_test_adds_no_failure_note() -> None:
    notes = runner._notes(("rest",), (), success=True)
    assert not any("joint test failed" in note for note in notes)
    assert not any(note.startswith("The guess run vetoed") for note in notes)


def test_a_candidate_set_other_than_the_production_set_says_it_is_a_test() -> None:
    assert runner._candidate_notes(("rest",)) == (
        "This candidate set is a test. Nothing ships from it, and the live model reads only the "
        "production features.",
    )
    assert runner._candidate_notes(FEATURES) == ()


def test_an_unfinished_season_is_named_with_its_count() -> None:
    coverage = [SeasonCount(2, 2, 40, 82, 3), SeasonCount(3, 2, 40, 82, 0)]
    assert runner._eligibility_notes(["too few games"], coverage) == (
        "Not eligible for success: too few games.",
        "Season 2 game type 2 holds 3 stored game(s) that are not final long after their start.",
    )


def test_a_selection_with_no_games_says_how_to_backfill() -> None:
    with pytest.raises(HockeyModelError) as caught:
        runner._validate([], [], PLAN, FEATURES)
    assert str(caught.value) == (
        f"backtest: no games in the selection seasons {PLAN.select_train} and "
        f"{PLAN.select_validate}; {REFRESH}"
    )


def test_a_scored_season_with_no_earlier_games_says_how_to_backfill() -> None:
    first: Any = SimpleNamespace(game=SimpleNamespace(season=PLAN.scored[0]))
    with pytest.raises(HockeyModelError) as caught:
        runner._fit_windows([first], PLAN)
    assert str(caught.value) == (
        f"backtest: no games to fit before season {PLAN.scored[0]}; {REFRESH}"
    )


def test_an_empty_store_says_how_to_backfill(store: sqlite3.Connection) -> None:
    store.commit()
    with pytest.raises(HockeyModelError) as caught:
        runner.run_backtest(store, PLAN, candidates=FEATURES)
    assert str(caught.value).endswith(REFRESH)


def _rows(plan_seasons: tuple[int, ...]) -> Any:
    """Return stand-in rows that carry only a season."""
    return [SimpleNamespace(game=SimpleNamespace(season=s)) for s in plan_seasons]


@pytest.mark.parametrize(("offset", "vetoed"), [(0.0, False), (1e-9, False), (2e-9, True)])
def test_a_feature_is_vetoed_only_when_its_guess_score_is_worse_by_more_than_the_tolerance(
    monkeypatch: pytest.MonkeyPatch, offset: float, vetoed: bool
) -> None:
    baseline = 0.5

    def unit(train, validate_actual, validate_guess, features):
        return {"guess": baseline + offset if features else baseline, "actual": baseline}

    monkeypatch.setattr(runner, "_validation_unit", unit)
    monkeypatch.setattr(runner, "_refused_in_windows", lambda *args: set())
    rows = _rows((PLAN.select_train[0], PLAN.select_validate[0]))
    _, got = runner._validate(rows, rows, PLAN, ("rest",))
    assert (got == ("rest",)) == vetoed


def test_a_score_exactly_at_the_tolerance_is_not_a_veto(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = 0.5

    def unit(train, validate_actual, validate_guess, features):
        score = baseline + runner.VETO_TOLERANCE if features else baseline
        return {"guess": score, "actual": baseline}

    monkeypatch.setattr(runner, "_validation_unit", unit)
    monkeypatch.setattr(runner, "_refused_in_windows", lambda *args: set())
    rows = _rows((PLAN.select_train[0], PLAN.select_validate[0]))
    assert runner._validate(rows, rows, PLAN, ("rest",))[1] == ()


def test_the_fits_of_a_selection_run_in_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real = runner.run_units

    def spy(units, workers):
        seen.append(workers)
        return real(units, workers)

    monkeypatch.setattr(runner, "run_units", spy)
    monkeypatch.setattr(runner, "_validation_unit", lambda *a: {"guess": 0.5, "actual": 0.5})
    monkeypatch.setattr(runner, "_refused_in_windows", lambda *args: set())
    rows = _rows((PLAN.select_train[0], PLAN.select_validate[0]))
    runner._validate(rows, rows, PLAN, ("rest",))
    assert seen == [1]


@pytest.fixture
def seeded(store: sqlite3.Connection) -> sqlite3.Connection:
    seed(store, range(1, 8))
    store.commit()
    return store


def _force(monkeypatch: pytest.MonkeyPatch, upper: float) -> None:
    """Make the model run's interval end at ``upper`` and the cohort eligible."""
    real = runner._scored_run

    def scored(name, probs, wins, elo_losses, tail):
        run = real(name, probs, wins, elo_losses, tail)
        if name == runner.MODEL_ACTUAL:
            return dataclasses.replace(run, diff_vs_elo_ci=(-1.0, upper))
        return run

    monkeypatch.setattr(runner, "_scored_run", scored)
    monkeypatch.setattr(runner, "ineligibility_reasons", lambda *args: [])


@pytest.mark.parametrize(("upper", "success"), [(-1e-9, True), (0.0, False), (1e-9, False)])
def test_success_needs_the_upper_end_of_the_interval_below_zero(
    seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, upper: float, success: bool
) -> None:
    _force(monkeypatch, upper)
    report, _ = runner.run_backtest(seeded, PLAN, candidates=FEATURES)
    assert report.success is success
    failed = "The joint test failed, so the Elo alone ships."
    assert (failed in report.notes) is (not success)
    assert bool(report.live_features) is success


def test_a_run_with_no_moneypuck_feature_carries_no_moneypuck_credit(
    seeded: sqlite3.Connection,
) -> None:
    report, _ = runner.run_backtest(seeded, PLAN, candidates=("rest",))
    assert report.tested_features == ("rest",)
    assert report.credits == ()
    assert runner.MONEYPUCK_LEAK_NOTE not in report.notes


def test_a_run_with_a_moneypuck_feature_carries_the_credit_and_the_leak_note(
    seeded: sqlite3.Connection,
) -> None:
    report, _ = runner.run_backtest(seeded, PLAN, candidates=("xg_form", "rest"))
    assert report.credits == (runner.MONEYPUCK_CREDIT,)
    assert runner.MONEYPUCK_LEAK_NOTE in report.notes


def test_the_report_holds_the_imputation_share_of_each_tested_feature(
    seeded: sqlite3.Connection,
) -> None:
    report, _ = runner.run_backtest(seeded, PLAN, candidates=FEATURES)
    assert [item["feature"] for item in report.imputation] == list(report.tested_features)
    assert all(set(item) == {"feature", "imputed", "total", "share"} for item in report.imputation)


def test_each_record_names_the_teams_of_its_game(seeded: sqlite3.Connection) -> None:
    _, records = runner.run_backtest(seeded, PLAN, candidates=FEATURES)
    assert records
    assert {(r.home, r.away) for r in records} <= {("TOR", "BOS"), ("BOS", "TOR")}
    assert {r.run for r in records} == set(runner.RECORDED_RUNS)


def test_a_serial_run_logs_each_build_by_name_and_all_stages_in_this_process(
    seeded: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workers: list[int] = []
    real = runner.run_units

    def spy(units, count):
        workers.append(count)
        return real(units, count)

    monkeypatch.setattr(runner, "run_units", spy)
    runner.run_backtest(seeded, PLAN, candidates=FEATURES)
    err = capsys.readouterr().err
    assert "backtest: unit build actual rows took " in err
    assert "backtest: unit build guess rows took " in err
    assert set(workers) == {1}


def test_the_total_line_states_the_workers_and_a_sane_time(
    seeded: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    runner.run_backtest(seeded, PLAN, candidates=FEATURES)
    total = [line for line in capsys.readouterr().err.splitlines() if "total" in line]
    assert len(total) == 1
    match = re.fullmatch(r"backtest: total ([0-9.]+)s with 1 worker\(s\)", total[0])
    assert match is not None, total[0]
    assert float(match.group(1)) < 600


def test_a_store_with_no_scored_games_says_how_to_backfill(store: sqlite3.Connection) -> None:
    seed(store, range(1, 6))
    store.commit()
    with pytest.raises(HockeyModelError) as caught:
        runner.run_backtest(store, PLAN, candidates=FEATURES)
    message = str(caught.value)
    assert message.startswith("backtest: no games in the scored ")
    assert message.endswith(REFRESH)


def test_a_run_whose_game_count_differs_from_the_elo_run_is_refused() -> None:
    with pytest.raises(ValueError, match="shorter"):
        runner._scored_run("model", [0.5, 0.6], [True, False], [0.7], 0.05)


def test_a_result_count_that_differs_from_the_job_count_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "run_units", lambda units, workers: [])
    rows = _rows((PLAN.select_train[0], PLAN.select_validate[0]))
    with pytest.raises(ValueError, match="shorter"):
        runner._validate(rows, rows, PLAN, ("rest",))
