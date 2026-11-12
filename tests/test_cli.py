"""Tests for the thin JSON CLI wrapper."""

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from seeding import add_game, add_goalie, add_goalie_form, add_team_form

from hockey_model import cli
from hockey_model.backtest.game_records import GameRecord
from hockey_model.backtest.metrics import calibration
from hockey_model.backtest.runner import BacktestReport, ScoredRun
from hockey_model.cli import CliRuntime
from hockey_model.game_model.features import ALL_FEATURE_NAMES
from hockey_model.identity import Catalog
from hockey_model.models import FixedStatModel
from hockey_model.sources.errors import SourceError
from hockey_model.store.db import connect
from hockey_model.store_cli import parse_features
from hockey_model.types import HockeyModelError, Player, Projection, Roster, Slot, Standing

PLAYER_PROJECTION = Projection(0.4, 0.1, 0.7, 0.8)


def _player(canonical_id: str, name: str = "A Player", *, yahoo_id: str | None = None) -> Player:
    return Player(canonical_id, yahoo_id, None, name, frozenset({Slot.C}), "TOR")


class Joint:
    def project_total(
        self, players: Sequence[Player], stat: str, horizon_games: int, level: float
    ) -> Projection:
        return Projection(float(len(players)), 0.0, 2.0, level)


class Game:
    def win_probabilities(self, home: Roster, away: Roster) -> tuple[float, float]:
        return 0.6, 0.4

    def expected_goals(self, team: Roster, level: float) -> Projection:
        return Projection(3.0, 1.0, 5.0, level)


class Playoffs:
    def playoff_odds(
        self, team_code: str, standings: Sequence[Standing], level: float
    ) -> Projection:
        return Projection(0.7, 0.5, 0.9, level)


def _runtime(players: tuple[Player, ...]) -> CliRuntime:
    table = {(player.canonical_id, "goals"): PLAYER_PROJECTION for player in players}
    return CliRuntime(
        Catalog(players),
        FixedStatModel(table),
        Joint(),
        Game(),
        Playoffs(),
        (Standing("TOR", 10, 12, 5, 8),),
    )


def _write_roster(path: Path, player_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "name": player_id,
                "players": [
                    {
                        "canonical_id": player_id,
                        "full_name": player_id,
                        "eligible": ["C"],
                        "team_code": "TOR",
                    }
                ],
                "slots": {player_id: "C"},
            }
        )
    )


def test_player_subcommand_prints_projection_json(capsys: pytest.CaptureFixture[str]) -> None:
    runtime = _runtime((_player("p-a"),))
    assert cli.main(["player", "goals", "p-a"], runtime) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"high": 0.7, "level": 0.8, "low": 0.1, "point": 0.4}


def test_ambiguous_name_exits_two_and_names_candidates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime((_player("p-a", "Sam Smith"), _player("p-b", "Sam Smith")))
    assert cli.main(["player", "goals", "Sam Smith"], runtime) == 2
    error = capsys.readouterr().err
    assert "p-a" in error and "p-b" in error


def test_unknown_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["nonsense"]) == 2
    assert "invalid choice" in capsys.readouterr().err


def test_known_command_without_runtime_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["player", "goals", "p-a"]) == 2
    assert "no models are configured" in capsys.readouterr().err


def test_player_subcommand_resolves_numeric_string_id(capsys: pytest.CaptureFixture[str]) -> None:
    player = _player("p-a", yahoo_id="123")
    assert cli.main(["player", "goals", "123"], _runtime((player,))) == 0
    assert json.loads(capsys.readouterr().out)["point"] == 0.4


def test_team_matchup_and_playoff_commands_print_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    player = _player("p-a")
    runtime = _runtime((player,))
    home = tmp_path / "home.json"
    away = tmp_path / "away.json"
    _write_roster(home, "p-a")
    _write_roster(away, "p-b")

    assert cli.main(["team", "goals", str(home)], runtime) == 0
    assert json.loads(capsys.readouterr().out)["total"]["point"] == 1.0
    assert cli.main(["matchup", str(home), str(away)], runtime) == 0
    assert json.loads(capsys.readouterr().out)["home_win"] == 0.6
    assert cli.main(["playoffs", "TOR"], runtime) == 0
    assert json.loads(capsys.readouterr().out)["point"] == 0.7


def test_bad_roster_file_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    assert cli.main(["team", "goals", str(bad)], _runtime((_player("p-a"),))) == 2
    assert "root must be" in capsys.readouterr().err


@pytest.mark.parametrize(
    "change",
    [
        {"slots": []},
        {"players": "not-an-array"},
        {"players": [{"canonical_id": None, "full_name": "x", "eligible": ["C"]}]},
        {"players": [{"canonical_id": "x", "full_name": [], "eligible": ["C"]}]},
        {"players": [{"canonical_id": "x", "full_name": "x", "eligible": ["C"], "nhl_id": True}]},
    ],
)
def test_malformed_roster_fields_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], change: dict[str, object]
) -> None:
    roster = {"name": "bad", "players": [], "slots": None} | change
    path = tmp_path / "bad-fields.json"
    path.write_text(json.dumps(roster))
    assert cli.main(["team", "goals", str(path)], _runtime((_player("p-a"),))) == 2
    assert "invalid roster fields" in capsys.readouterr().err


def test_roster_property_parses_valid_dicts(tmp_path: Path) -> None:
    for name, cid, fname in [
        ("team-a", "p-1", "Player One"),
        ("team-b", "p-2", "Player Two"),
        ("team-c", "p-3", "Player Three"),
    ]:
        roster = {
            "name": name,
            "players": [{"canonical_id": cid, "full_name": fname, "eligible": ["C"]}],
            "slots": None,
        }
        path = tmp_path / f"{cid}.json"
        path.write_text(json.dumps(roster))
        result = cli._roster(path)
        assert isinstance(result, Roster)
        assert result.name == name


def test_empty_player_reference_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    runtime = _runtime((_player("p-a"),))
    assert cli.main(["player", "goals", ""], runtime) == 2
    assert "must not be empty" in capsys.readouterr().err


class _RefreshClient:
    """Stands in for HttpClient: every request fails, so each source reports an error."""

    def get(self, source: str, url: str) -> bytes:
        raise SourceError(source, url, "HTTP 503")


def test_cli_refresh_requires_db_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HOCKEY_MODEL_DB", raising=False)
    assert cli.main(["refresh"]) == 2
    assert "HOCKEY_MODEL_DB" in capsys.readouterr().err


def test_cli_refresh_exits_one_on_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    monkeypatch.setattr("hockey_model.store_cli.HttpClient", _RefreshClient)
    code = cli.main(["refresh", "--backfill-from", "2026-01-10"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["errors"]
    assert report["errors"][0]["type"] == "SourceError"
    assert report["credits"] == []


def test_cli_refresh_rejects_bad_backfill_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["refresh", "--backfill-from", "yesterday"]) == 2


def test_cli_refresh_rejects_a_budget_below_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["refresh", "--budget-seconds", "0"]) == 2
    assert "--budget-seconds" in capsys.readouterr().err


def test_cli_refresh_rejects_a_budget_with_starters_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["refresh", "--starters-only", "--budget-seconds", "60"]) == 2
    assert "--starters-only" in capsys.readouterr().err


def test_cli_refresh_rejects_a_budget_without_a_backfill(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["refresh", "--budget-seconds", "60"]) == 2
    assert "--budget-seconds needs --backfill-from" in capsys.readouterr().err


# A fit refuses a feature imputed in more than the limit of rows. The first game of each team
# has no earlier form or rest, so the store needs enough games and form for every game.
STORE_GAMES = 43


def _store_with_games(path: Path) -> None:
    conn = connect(path)
    add_game(conn, 2015020001, "2015-11-01T00:00", season=20152016)
    add_game(conn, 2015020002, "2015-11-03T00:00", season=20152016, home="BOS", away="TOR")
    add_game(conn, 2015020003, "2015-11-05T00:00", season=20152016, score=(1, 4))
    history = range(2015020010, 2015020010 + STORE_GAMES - 3)
    for n, game_id in enumerate(history):
        start = datetime(2015, 11, 6, tzinfo=UTC) + timedelta(days=n)
        home, away = ("TOR", "BOS") if n % 2 else ("BOS", "TOR")
        stamp = start.strftime("%Y-%m-%dT%H:%M")
        add_game(conn, game_id, stamp, season=20152016, home=home, away=away)
    for game_id in (2015020001, 2015020002, 2015020003, *history):
        add_team_form(conn, game_id, "TOR", 2.6, 2.5)
        add_team_form(conn, game_id, "BOS", 2.5, 2.6)
    add_game(conn, 2025020001, "2026-12-01T00:00", state="FUT", season=20262027)
    conn.commit()
    conn.close()


def test_cli_fit_requires_through_season(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["fit"]) == 2


def test_cli_fit_rejects_unknown_feature(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    code = cli.main(["fit", "--through-season", "20152016", "--features", "luck"])
    assert code == 2
    assert "luck" in capsys.readouterr().err


def test_cli_fit_with_no_games_says_to_refresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["fit", "--through-season", "20152016", "--features", "rest"]) == 2
    assert "hockey-model refresh" in capsys.readouterr().err


def test_cli_fit_saves_a_fit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    db = tmp_path / "store.db"
    _store_with_games(db)
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    code = cli.main(["fit", "--through-season", "20152016", "--features", "rest"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["games"] == STORE_GAMES
    assert out["features"] == ["rest"]
    assert out["through_season"] == 20152016
    assert out["diagnostics"]["gradient_norm"] < 1e-6
    assert out["diagnostics"]["stop_reason"].startswith("converged")


def test_cli_fit_above_the_imputation_limit_exits_two_and_saves_no_fit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    db = tmp_path / "store.db"
    conn = connect(db)
    add_game(conn, 2015020001, "2015-11-01T00:00", season=20152016)
    add_game(conn, 2015020002, "2015-11-03T00:00", season=20152016, home="BOS", away="TOR")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    code = cli.main(["fit", "--through-season", "20152016", "--features", "xg_form"])
    assert code == 2
    assert "feature refused" in capsys.readouterr().err
    conn = connect(db)
    saved = conn.execute("SELECT COUNT(*) FROM model_fits").fetchone()[0]
    conn.close()
    assert saved == 0


def test_cli_fit_with_empty_features_saves_the_elo_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    code = cli.main(["fit", "--through-season", "20252026", "--features", ""])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (out["features"], out["elo_weight"], out["intercept"]) == ([], 1.0, 0.0)
    assert out["diagnostics"] is None


def test_cli_fit_without_features_is_an_argument_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """No default feature list: a fit never silently includes a vetoed candidate."""
    db = tmp_path / "store.db"
    _store_with_games(db)
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    assert cli.main(["fit", "--through-season", "20152016"]) == 2
    err = capsys.readouterr().err
    assert "--features" in err
    assert "backtest" in err
    conn = connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM model_fits").fetchone()[0] == 0
    finally:
        conn.close()


def test_cli_fit_rejects_a_feature_named_twice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    code = cli.main(["fit", "--through-season", "20152016", "--features", "rest,rest"])
    assert code == 2
    assert "'rest'" in capsys.readouterr().err


@pytest.mark.parametrize("text", [",", "xg_form,,rest", "rest,", " ", ",rest"])
def test_cli_fit_rejects_a_blank_feature_entry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, text: str
) -> None:
    """A blank entry changes the feature set without a word, so it is an error."""
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["fit", "--through-season", "20152016", "--features", text]) == 2
    assert "blank entry" in capsys.readouterr().err


def test_feature_names_ignore_spaces_around_names() -> None:
    assert parse_features(" xg_form , rest ") == ("xg_form", "rest")


@given(st.lists(st.sampled_from(ALL_FEATURE_NAMES), unique=True, max_size=4))
def test_feature_names_round_trip_any_distinct_known_list(names: list[str]) -> None:
    assert parse_features(",".join(names)) == tuple(names)


@given(st.text(max_size=30))
def test_feature_names_return_distinct_known_names_or_raise(text: str) -> None:
    try:
        names = parse_features(text)
    except HockeyModelError:
        return
    assert len(set(names)) == len(names)
    assert all(name in ALL_FEATURE_NAMES for name in names)


def test_cli_fit_accepts_the_attempt_form_research_recipe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A research fit stays possible, and the saved fit predicts."""
    db = tmp_path / "store.db"
    _store_with_games(db)
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    assert cli.main(["fit", "--through-season", "20152016", "--features", "attempt_form"]) == 0
    capsys.readouterr()
    assert cli.main(["game", "2025020001"]) == 0


def test_the_selected_recipe_leaves_the_vetoed_feature_out_of_the_served_fit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The quick start: the recipe from the backtest report is the fit the model serves."""
    db = tmp_path / "store.db"
    _store_with_games(db)
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    assert cli.main(["fit", "--through-season", "20152016", "--features", "xg_form,rest"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["features"] == ["xg_form", "rest"]
    assert "goalie_gsax" not in out["features"]
    assert cli.main(["game", "2025020001"]) == 0


def test_an_empty_live_recipe_serves_the_raw_elo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A failed joint test gives live_features of (). That fit is the fixed Elo, not a refit."""
    db = tmp_path / "store.db"
    _store_with_games(db)
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    assert cli.main(["fit", "--through-season", "20152016", "--features", ""]) == 0
    fit = json.loads(capsys.readouterr().out)
    assert (fit["features"], fit["elo_weight"], fit["intercept"]) == ([], 1.0, 0.0)
    assert fit["games"] == 0
    assert cli.main(["game", "2025020001"]) == 0


def test_cli_game_prints_prediction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    db = tmp_path / "store.db"
    _store_with_games(db)
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    assert cli.main(["fit", "--through-season", "20152016", "--features", "rest"]) == 0
    capsys.readouterr()
    assert cli.main(["game", "2025020001", "--level", "0.9"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["home_win"] + out["away_win"] == pytest.approx(1.0)
    assert out["home_goals"]["level"] == 0.9
    assert out["made_after_start"] is False
    assert isinstance(out["missing"], list)


def test_cli_game_unknown_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["game", "123"]) == 2
    err = capsys.readouterr().err
    assert "123" in err
    assert "hockey-model refresh" in err


def test_cli_game_without_a_fit_says_to_fit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    db = tmp_path / "store.db"
    _store_with_games(db)
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    assert cli.main(["game", "2025020001"]) == 2
    assert "hockey-model fit" in capsys.readouterr().err


def test_cli_backtest_without_games_says_to_refresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["backtest"]) == 2
    assert "hockey-model refresh" in capsys.readouterr().err


def _fake_backtest(success: bool) -> object:
    run = ScoredRun("elo", 2, 0.69, 0.25, calibration([0.5, 0.6], [True, False]), None)
    report = BacktestReport(
        ("xg_form",),
        (),
        ("xg_form",),
        ("xg_form",) if success else (),
        {"elo": {"actual": 0.7, "guess": 0.7}},
        (run,),
        success,
        ("note",),
        (),
        {"xg_form": 3},
    )
    record = GameRecord(1, datetime(2026, 1, 1, tzinfo=UTC), "TOR", "BOS", "elo", 0.55, True)
    return lambda conn, **_: (report, [record])


@pytest.mark.parametrize("success", [True, False])
def test_cli_backtest_prints_report_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    success: bool,
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    monkeypatch.setattr("hockey_model.store_cli.run_backtest", _fake_backtest(success))
    assert cli.main(["backtest"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is success
    assert out["live_features"] == (["xg_form"] if success else [])
    assert out["missing_inputs"] == {"xg_form": 3}
    empty_bin = out["runs"][0]["calibration"][0]
    assert empty_bin["count"] == 0
    assert empty_bin["mean_prob"] is None


def test_cli_backtest_writes_games_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    monkeypatch.setattr("hockey_model.store_cli.run_backtest", _fake_backtest(True))
    target = tmp_path / "games.csv"
    assert cli.main(["backtest", "--games-out", str(target)]) == 0
    capsys.readouterr()
    lines = target.read_text().splitlines()
    assert lines[0] == "game_id,start,home,away,run,home_win_prob,home_won"
    assert lines[1].startswith("1,2026-01-01T00:00:00.000000+00:00,TOR,BOS,elo,0.55,True")


def test_cli_backtest_games_out_to_a_bad_path_names_the_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    monkeypatch.setattr("hockey_model.store_cli.run_backtest", _fake_backtest(True))
    target = tmp_path / "missing-dir" / "games.csv"
    assert cli.main(["backtest", "--games-out", str(target)]) == 2
    err = capsys.readouterr().err
    assert "FileNotFoundError" in err
    assert "check the file path" in err


def test_an_unexpected_store_failure_names_its_type_and_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(args: object) -> tuple[object, int]:
        raise RuntimeError("the table is gone")

    monkeypatch.setattr(cli, "run_store_command", boom)
    assert cli.main(["refresh"]) == 2
    err = capsys.readouterr().err
    assert "RuntimeError: the table is gone" in err
    assert "model configuration" not in err


_EXPERIMENT_RECORD = {
    "id": "0001-cli",
    "title": "CLI record",
    "hypothesis": "Feature X improves log loss.",
    "cohort": "exploration",
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


def _registry(tmp_path: Path) -> Path:
    """Make a temporary registry directory for the CLI to read."""
    registry = tmp_path / "experiments"
    registry.mkdir()
    return registry


def _write_record(registry: Path, data: dict) -> Path:
    """Put one record in the registry directory."""
    path = registry / f"{data['id']}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _seed_seasons(conn, seasons: range) -> None:
    """Put 20 final games in each season, with form and a starter, so a fit measures them."""
    game_id = 0
    for season in seasons:
        for day in range(1, 21):
            game_id += 1
            start = f"{season // 10000:04d}-11-{day:02d}T00:00"
            add_game(conn, game_id, start, season=season)
            add_team_form(conn, game_id, "TOR", 2.6, 2.5)
            add_team_form(conn, game_id, "BOS", 2.5, 2.6)
            add_goalie(conn, game_id, "TOR", 11, "G TOR")
            add_goalie(conn, game_id, "BOS", 21, "G BOS")
            add_goalie_form(conn, game_id, 11, 2.5, 2)
            add_goalie_form(conn, game_id, 21, 2.5, 2)
    conn.commit()


def test_cli_experiments_check_exits_zero_for_a_valid_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _registry(tmp_path)
    _write_record(registry, _EXPERIMENT_RECORD)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["experiments", "check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["records"][0]["id"] == "0001-cli"


def test_cli_experiments_check_exits_one_and_names_the_bad_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _registry(tmp_path)
    _write_record(registry, {**_EXPERIMENT_RECORD, "min_games": "ten"})
    monkeypatch.chdir(tmp_path)
    assert cli.main(["experiments", "check"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "min_games" in payload["errors"][0]


def test_cli_experiments_without_a_subcommand_exits_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["experiments"]) == 2
    assert "specify check or run" in capsys.readouterr().err


def test_cli_experiments_run_names_a_missing_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _registry(tmp_path)
    _write_record(registry, _EXPERIMENT_RECORD)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert cli.main(["experiments", "run", "0002-absent"]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_experiments_run_scores_an_exploration_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _registry(tmp_path)
    _write_record(registry, _EXPERIMENT_RECORD)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "store.db"
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    conn = connect(db)
    _seed_seasons(conn, range(20152016, 20262027, 10001))
    conn.close()
    assert cli.main(["experiments", "run", "0001-cli"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["scored"] == "seasons 20162017 to 20252026"
    assert payload["status"] == "registered"


def test_playoff_invalid_probability_exits_two_without_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class InvalidPlayoffs:
        def playoff_odds(
            self, team_code: str, standings: Sequence[Standing], level: float
        ) -> Projection:
            return Projection(1.2, 1.1, 1.3, level)

    runtime = _runtime((_player("p-a"),))
    runtime = replace(runtime, standings_model=InvalidPlayoffs())
    assert cli.main(["playoffs", "TOR"], runtime) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "TOR" in output.err and "[0, 1]" in output.err


class _RaisingStatModel:
    """A stat model whose bug is a TypeError."""

    def project(self, player: Player, stat: str, horizon_games: int, level: float) -> Projection:
        raise TypeError("model sentinel")


def test_a_model_type_error_is_an_unexpected_failure_not_a_serialization_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = replace(_runtime((_player("p-a"),)), stat_model=_RaisingStatModel())
    assert cli.main(["player", "goals", "p-a"], runtime) == 2
    captured = capsys.readouterr()
    assert captured.err == "error: unexpected failure: TypeError: model sentinel\n"
    assert captured.out == ""


def test_an_unsupported_payload_is_a_serialization_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_run", lambda args, runtime: {"value": object()})
    runtime = _runtime((_player("p-a"),))
    assert cli.main(["player", "goals", "p-a"], runtime) == 2
    captured = capsys.readouterr()
    assert captured.err == "error: serialization failed: cannot serialize object\n"
    assert captured.out == ""


def test_a_malformed_model_projection_exits_two_without_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class WrongLevel:
        def project(
            self, player: Player, stat: str, horizon_games: int, level: float
        ) -> Projection:
            return Projection(0.4, 0.1, 0.7, 0.5)

    runtime = replace(_runtime((_player("p-a"),)), stat_model=WrongLevel())
    assert cli.main(["player", "goals", "p-a"], runtime) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "the projection has level 0.5 but 0.8 was requested" in output.err


def test_cli_game_reports_a_degraded_form_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    db = tmp_path / "store.db"
    _store_with_games(db)
    # MTL has no earlier game, so only its form is missing for this game.
    conn = connect(db)
    add_game(
        conn, 2025020002, "2026-12-02T00:00", state="FUT", season=20262027, home="MTL", away="TOR"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    assert cli.main(["fit", "--through-season", "20152016", "--features", "xg_form"]) == 0
    capsys.readouterr()
    assert cli.main(["game", "2025020002"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["degraded_inputs"] == [{"feature": "xg_form", "kind": "xg_form", "teams": ["MTL"]}]
