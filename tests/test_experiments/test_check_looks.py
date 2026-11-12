"""Tests for the look count, the cohort check, and the data_cutoff instant in a run."""

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from experiment_support import ENOUGH_GAMES, FIT_SEASONS, register, seed
from seeding import add_game, add_goalie, add_goalie_form, add_team_form

from hockey_model.backtest import runner
from hockey_model.experiments import check
from hockey_model.experiments.check import check_all, run_experiment
from hockey_model.experiments.cohort import cutoff_instant
from hockey_model.experiments.looks import BASE_TAIL
from hockey_model.experiments.models import ExperimentRecord, HockeyModelError


def _prepare(store, cutoff: str = "2026-10-31") -> None:
    seed(store, FIT_SEASONS)
    seed(store, [20262027], first_id=1000, games=ENOUGH_GAMES)


class TestRegistryLookNumbers:
    """check_all enforces the look number rule on confirmation records."""

    def test_a_second_confirmation_record_at_look_one_fails(self, registry_dir: Path):
        register(registry_dir, {"id": "0001-first"})
        register(registry_dir, {"id": "0002-second", "look_number": 1})
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert any("0002-second" in e and "expected 2" in e for e in result["errors"])

    def test_a_second_confirmation_record_at_look_two_passes(self, registry_dir: Path):
        register(registry_dir, {"id": "0001-first"})
        register(registry_dir, {"id": "0002-second", "look_number": 2})
        assert check_all(repo_root=registry_dir.parent)["ok"] is True

    def test_an_exploration_record_does_not_raise_the_look(self, registry_dir: Path):
        register(registry_dir, {"id": "0001-explore", "cohort": "exploration"})
        register(registry_dir, {"id": "0002-confirm", "look_number": 1})
        assert check_all(repo_root=registry_dir.parent)["ok"] is True

    def test_a_look_above_the_schema_maximum_fails(self, registry_dir: Path):
        register(registry_dir, {"id": "0001-first", "look_number": 11})
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False


class TestRunUsesTheLook:
    """run_experiment scores a later look against a stricter bar."""

    @pytest.mark.parametrize("look", [1, 3])
    def test_the_interval_tail_is_the_base_tail_over_the_look(
        self, registry_dir: Path, store, monkeypatch: pytest.MonkeyPatch, look: int
    ):
        _prepare(store)
        for n in range(1, look):
            register(registry_dir, {"id": f"{n:04d}-earlier", "look_number": n})
        register(registry_dir, {"id": "0009-now", "look_number": look})
        seen = []
        real = runner.run_backtest

        def spy(conn, plan, **kwargs):
            seen.append(plan.interval_tail)
            return real(conn, plan, **kwargs)

        monkeypatch.setattr(check, "run_backtest", spy)
        run_experiment("0009-now", store, repo_root=registry_dir.parent)
        assert seen == [pytest.approx(BASE_TAIL / look)]

    def test_a_wrong_look_number_refuses_before_any_run(self, registry_dir: Path, store):
        _prepare(store)
        register(registry_dir, {"id": "0001-first"})
        register(registry_dir, {"id": "0002-second", "look_number": 1})
        with pytest.raises(HockeyModelError, match="expected 2"):
            run_experiment("0002-second", store, repo_root=registry_dir.parent)
        saved = ExperimentRecord.load(registry_dir / "0002-second.json")
        assert saved.status == "registered"


class TestScoredCohort:
    """A run checks the scored games against the cohort it counted."""

    def test_a_run_that_scores_fewer_games_than_the_cohort_fails_and_saves_nothing(
        self, registry_dir: Path, store, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare(store)
        register(registry_dir, {"id": "0001-first"})
        real = runner.run_backtest

        def short(conn, plan, **kwargs):
            report, records = real(conn, plan, **kwargs)
            runs = tuple(
                dataclasses.replace(r, games=r.games - 1) if r.name == runner.MODEL_ACTUAL else r
                for r in report.runs
            )
            return dataclasses.replace(report, runs=runs), records

        monkeypatch.setattr(check, "run_backtest", short)
        with pytest.raises(HockeyModelError, match="scored .* games.*cohort holds"):
            run_experiment("0001-first", store, repo_root=registry_dir.parent)
        assert ExperimentRecord.load(registry_dir / "0001-first.json").status == "registered"
        assert not (registry_dir / "results" / "0001-first-games.csv").exists()


class TestCutoffInstant:
    """data_cutoff names the last day of data, so that day's games are not in the cohort."""

    def test_the_cutoff_instant_is_the_last_microsecond_of_the_day_utc(self, registry_dir: Path):
        record = register(registry_dir, {"data_cutoff": "2026-10-31"})
        assert cutoff_instant(record) == datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=UTC)

    def test_a_game_on_the_cutoff_day_is_not_scored(self, registry_dir: Path, store):
        _prepare(store)
        add_game(store, 4000, "2026-10-31T23:00", season=20262027)
        add_game(store, 4001, "2026-11-01T00:00", season=20262027)
        for gid in (4000, 4001):
            add_team_form(store, gid, "TOR", 2.6, 2.5)
            add_team_form(store, gid, "BOS", 2.5, 2.6)
            add_goalie(store, gid, "TOR", 11, "G TOR")
            add_goalie(store, gid, "BOS", 21, "G BOS")
            add_goalie_form(store, gid, 11, 2.5, 2)
            add_goalie_form(store, gid, 21, 2.5, 2)
        register(registry_dir, {"id": "0001-first", "min_games": 15})
        _, entries = run_experiment("0001-first", store, repo_root=registry_dir.parent)
        ids = {entry["game_id"] for entry in entries}
        assert 4000 not in ids
        assert 4001 in ids

    def test_a_cutoff_that_is_not_a_date_names_the_record(self, registry_dir: Path):
        record = dataclasses.replace(register(registry_dir), data_cutoff="soon")
        with pytest.raises(HockeyModelError, match="0001-valid.*YYYY-MM-DD"):
            cutoff_instant(record)


class TestSeasonFloorNote:
    """The payload says when the season floor moves the first scored season."""

    def test_an_early_cutoff_adds_a_note(self, registry_dir: Path, store):
        _prepare(store)
        register(registry_dir, {"id": "0001-first", "data_cutoff": "2025-10-31"})
        payload, _ = run_experiment("0001-first", store, repo_root=registry_dir.parent)
        assert any("20262027" in n and "2025-10-31" in n for n in payload["notes"])

    def test_a_cutoff_inside_the_first_season_adds_no_note(self, registry_dir: Path, store):
        _prepare(store)
        register(registry_dir, {"id": "0001-first"})
        payload, _ = run_experiment("0001-first", store, repo_root=registry_dir.parent)
        assert not any("data_cutoff" in n for n in payload["notes"])
