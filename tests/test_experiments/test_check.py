"""Tests for the experiment checker module.

Covers:
- check_all and check_experiment on a temporary registry and on the real registry
- registration commit lookup and immutability in a real git repository
- confirmation run rules: status, the one-shot guard, cohort size, cohort membership
- what a confirmation run writes: status, mean difference, per-game CSV, hash
"""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from experiment_support import (
    ENOUGH_GAMES,
    FIT_SEASONS,
    VALID_RECORD_DATA,
    make_record,
    register,
    seed,
)
from seeding import add_game

from hockey_model.backtest.runner import BacktestReport
from hockey_model.experiments.check import (
    check_all,
    record_with_result,
    run_experiment,
)
from hockey_model.experiments.history import (
    check_immutable_fields,
    check_one_shot,
    registration_commit,
)
from hockey_model.experiments.models import ExperimentRecord, HockeyModelError

# A record that already spent its look.
_SPENT = {
    "status": "rejected",
    "result": {
        "games": 20,
        "difference": 0.001,
        "interval": [0.0005, 0.002],
        "csv_path": "experiments/results/0002-other-games.csv",
        "csv_hash": "0" * 64,
    },
}


def _repo_root() -> Path:
    """Return the repository root, so the test works from any working directory."""
    return Path(_git(Path(__file__).parent, "rev-parse", "--show-toplevel").strip())


def _git(root: Path, *args: str) -> str:
    """Run git in the fixture repository and return its standard output."""
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _init_repo(root: Path) -> None:
    """Make a git repository with an identity and no signing, so commits work anywhere."""
    _git(root, "init")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.com")
    _git(root, "config", "commit.gpgsign", "false")


class TestCheckAll:
    """Test check_all on a temporary registry."""

    def test_check_all_empty(self, registry_dir: Path):
        """check_all returns ok when the registry is empty."""
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is True
        assert result["records"] == []
        assert result["errors"] == []

    def test_check_all_valid_record(self, registry_dir: Path):
        """A valid record passes, and immutability is reported as unchecked outside git."""
        register(registry_dir)
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is True
        entry = result["records"][0]
        assert entry["id"] == "0001-valid"
        assert entry["valid"] is True
        assert "git does not track" in entry["immutability"]

    def test_check_all_invalid_record(self, registry_dir: Path):
        """A record with a status the schema rejects makes check_all fail."""
        bad = make_record({"status": "invalid_status"})
        bad.save(registry_dir / f"{bad.id}.json")
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "not in" in result["errors"][0]

    def test_check_all_fails_when_a_result_csv_is_missing(self, registry_dir: Path):
        """A result that names a CSV the repository does not hold is an error."""
        register(
            registry_dir,
            {
                "status": "rejected",
                "result": {
                    "games": 100,
                    "difference": -0.005,
                    "interval": [-0.010, -0.001],
                    "csv_path": "experiments/results/0001-valid-games.csv",
                    "csv_hash": "0" * 64,
                },
            },
        )
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "not in the repository" in result["errors"][0]

    def test_check_all_fails_when_a_result_csv_hash_does_not_match(self, registry_dir: Path):
        """A per-game CSV that changed after the run fails the hash check."""
        csv_path = registry_dir / "results" / "0001-valid-games.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text("game_id,run\n1,elo\n", encoding="utf-8")
        register(
            registry_dir,
            {
                "status": "rejected",
                "result": {
                    "games": 1,
                    "difference": -0.001,
                    "interval": [-0.002, 0.000],
                    "csv_path": "experiments/results/0001-valid-games.csv",
                    "csv_hash": "0" * 64,
                },
            },
        )
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "does not match the hash" in result["errors"][0]

    def test_check_all_accepts_a_result_csv_whose_hash_matches(self, registry_dir: Path):
        """A result whose CSV still matches its hash passes."""
        csv_path = registry_dir / "results" / "0001-valid-games.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        text = "game_id,run\n1,elo\n"
        csv_path.write_text(text, encoding="utf-8")
        register(
            registry_dir,
            {
                "status": "rejected",
                "result": {
                    "games": 1,
                    "difference": -0.001,
                    "interval": [-0.002, 0.000],
                    "csv_path": "experiments/results/0001-valid-games.csv",
                    "csv_hash": hashlib.sha256(text.encode()).hexdigest(),
                },
            },
        )
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is True
        assert result["records"][0]["has_result"] is True

    def test_check_all_raises_when_the_registry_directory_is_missing(self, tmp_path: Path):
        """A directory with no registry is an error, not an empty success."""
        with pytest.raises(HockeyModelError, match="no experiment registry"):
            check_all(repo_root=tmp_path)

    def test_check_all_accepts_the_real_registry(self):
        """The records committed in the repository pass their own checker."""
        result = check_all(repo_root=_repo_root())
        assert result["ok"] is True, result["errors"]
        ids = [entry["id"] for entry in result["records"]]
        assert "0001-v1-forward-test" in ids


class TestRegistrationCommit:
    """Test the git lookup of the commit that registered a record."""

    def test_registration_commit_is_none_without_git(self, registry_dir: Path):
        """A registry that git does not track has no registration commit."""
        register(registry_dir)
        assert registration_commit("0001-valid", registry_dir.parent) is None

    def test_registration_commit_is_the_first_commit(self, tmp_path: Path, registry_dir: Path):
        """The registration commit is the oldest commit that touched the record."""
        root = tmp_path
        _init_repo(root)
        register(registry_dir)
        _git(root, "add", "experiments")
        _git(root, "commit", "-m", "register the record")
        first = _git(root, "rev-parse", "HEAD").strip()
        _git(root, "commit", "--allow-empty", "-m", "an unrelated commit")
        assert registration_commit("0001-valid", root) == first


class TestImmutableFields:
    """Test the immutability check against a real git repository."""

    def test_immutable_fields_pass_when_nothing_changed(self, tmp_path: Path, registry_dir: Path):
        """A record identical to its registered version passes."""
        root = tmp_path
        _init_repo(root)
        record = register(registry_dir)
        _git(root, "add", "experiments")
        _git(root, "commit", "-m", "register the record")
        commit = registration_commit("0001-valid", root)
        assert commit is not None
        check_immutable_fields(record, commit, root)

    def test_immutable_fields_raise_when_min_games_changed(
        self, tmp_path: Path, registry_dir: Path
    ):
        """Editing min_games after registration fails the check."""
        root = tmp_path
        _init_repo(root)
        registered = register(registry_dir)
        _git(root, "add", "experiments")
        _git(root, "commit", "-m", "register the record")
        commit = registration_commit("0001-valid", root)
        assert commit is not None
        edited = make_record({"min_games": 5000})
        edited.save(registry_dir / "0001-valid.json")
        _git(root, "commit", "-am", "quietly change min_games")
        with pytest.raises(HockeyModelError, match="immutable field\\(s\\) min_games changed"):
            check_immutable_fields(edited, commit, root)
        assert registered.status == "registered"

    def test_a_field_added_after_registration_may_gain_a_value(
        self, tmp_path: Path, registry_dir: Path
    ):
        """A field the record did not carry at registration is a schema addition, not a change."""
        root = tmp_path
        _init_repo(root)
        first = {k: v for k, v in VALID_RECORD_DATA.items() if k != "candidate_features"}
        (registry_dir / "0001-valid.json").write_text(json.dumps(first), encoding="utf-8")
        _git(root, "add", "experiments")
        _git(root, "commit", "-m", "register the record")
        commit = registration_commit("0001-valid", root)
        assert commit is not None
        current = make_record({"candidate_features": ["xg_form", "rest"]})
        check_immutable_fields(current, commit, root)

    def test_immutable_fields_raise_when_the_commit_has_no_record(
        self, tmp_path: Path, registry_dir: Path
    ):
        """A commit outside the record history is refused with a message that names it."""
        root = tmp_path
        _init_repo(root)
        _git(root, "commit", "--allow-empty", "-m", "an empty commit")
        commit = _git(root, "rev-parse", "HEAD").strip()
        record = register(registry_dir)
        with pytest.raises(HockeyModelError, match="is not in the history of the record"):
            check_immutable_fields(record, commit, root)


class TestStatusMatchesResult:
    """Test that the checker ties status to result."""

    _RESULT = {
        "games": 20,
        "difference": -0.001,
        "interval": [-0.002, -0.0005],
        "csv_path": "experiments/results/0001-valid-games.csv",
        "csv_hash": "0" * 64,
    }

    def test_promoted_without_a_result_fails(self, registry_dir: Path):
        """A record cannot claim promotion with no result behind it."""
        register(registry_dir, {"status": "promoted"})
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "holds no result" in result["errors"][0]

    def test_registered_with_a_result_fails(self, registry_dir: Path):
        """A registered record that holds a result was edited back by hand."""
        register(registry_dir, {"result": self._RESULT})
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "'registered' but the record holds a result" in result["errors"][0]

    def test_inconclusive_without_a_result_passes(self, registry_dir: Path):
        """A run with no interval is inconclusive and writes no result."""
        register(registry_dir, {"status": "inconclusive"})
        assert check_all(repo_root=registry_dir.parent)["ok"] is True


class TestOneShot:
    """Test the one-shot rule against a real git repository."""

    def _commit_result(self, root: Path, registry_dir: Path) -> ExperimentRecord:
        """Commit a registered record, then commit its result."""
        _init_repo(root)
        record = register(registry_dir)
        _git(root, "add", "experiments")
        _git(root, "commit", "-m", "register the record")
        data = record.to_dict()
        data["status"] = "rejected"
        data["result"] = {
            "games": 20,
            "difference": 0.001,
            "interval": [0.0005, 0.002],
            "csv_path": "experiments/results/0001-valid-games.csv",
            "csv_hash": "0" * 64,
        }
        with_result = ExperimentRecord.from_dict(data)
        with_result.save(registry_dir / "0001-valid.json")
        _git(root, "commit", "-am", "record the result")
        return with_result

    def test_a_kept_result_passes(self, tmp_path: Path, registry_dir: Path):
        """A record that still holds its committed result passes."""
        with_result = self._commit_result(tmp_path, registry_dir)
        check_one_shot(with_result, tmp_path)

    def test_a_reverted_result_fails(self, tmp_path: Path, registry_dir: Path):
        """Resetting a record to registered and null after a run fails the check."""
        with_result = self._commit_result(tmp_path, registry_dir)
        data = with_result.to_dict()
        data["status"] = "registered"
        data["result"] = None
        reverted = ExperimentRecord.from_dict(data)
        reverted.save(registry_dir / "0001-valid.json")
        _git(tmp_path, "commit", "-am", "revert the result")
        with pytest.raises(HockeyModelError, match="one-shot"):
            check_one_shot(reverted, tmp_path)

    def test_a_replaced_result_fails(self, tmp_path: Path, registry_dir: Path):
        """Swapping a committed result for another one fails the check."""
        with_result = self._commit_result(tmp_path, registry_dir)
        data = with_result.to_dict()
        data["result"] = {**data["result"], "difference": -0.01}
        swapped = ExperimentRecord.from_dict(data)
        swapped.save(registry_dir / "0001-valid.json")
        _git(tmp_path, "commit", "-am", "swap the result")
        with pytest.raises(HockeyModelError, match="one-shot"):
            check_one_shot(swapped, tmp_path)


class TestRegistryRules:
    """Test the checks that look at the whole registry or at a record's identity."""

    def test_file_name_must_match_the_record_id(self, registry_dir: Path):
        """Editing the id inside a record file cannot hide it from the history checks."""
        record = make_record({"id": "0009-renamed"})
        record.save(registry_dir / "0001-valid.json")
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "the file is named '0001-valid'" in result["errors"][0]

    def test_an_unknown_candidate_feature_fails(self, registry_dir: Path):
        """A typo in a frozen record is caught before the record is frozen."""
        register(registry_dir, {"candidate_features": ["xg_frm"]})
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "unknown feature 'xg_frm'" in result["errors"][0]

    def test_a_repeated_candidate_feature_fails(self, registry_dir: Path):
        """The checker applies the same rule as the fit command: each feature once."""
        register(registry_dir, {"candidate_features": ["rest", "rest"]})
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "'rest' is named more than once" in result["errors"][0]

    def test_two_records_cannot_share_a_number(self, registry_dir: Path):
        """The registry number is the record's slot, so a repeat is an error."""
        register(registry_dir, {"id": "0003-first"})
        register(registry_dir, {"id": "0003-second"})
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "registry number 0003 is used by more than one record" in result["errors"][0]

    def test_a_deleted_record_fails(self, tmp_path: Path, registry_dir: Path):
        """Removing a committed record from the working tree is an error."""
        _init_repo(tmp_path)
        register(registry_dir, {"id": "0001-valid"})
        register(registry_dir, _SPENT | {"id": "0002-other"})
        _git(tmp_path, "add", "experiments")
        _git(tmp_path, "commit", "-m", "register two records")
        (registry_dir / "0002-other.json").unlink()
        result = check_all(repo_root=tmp_path)
        assert result["ok"] is False
        assert any("0002-other: tracked" in error for error in result["errors"])

    def test_a_committed_deletion_fails(self, tmp_path: Path, registry_dir: Path):
        """A record removed in a later commit still counts: a committed number stays."""
        _init_repo(tmp_path)
        register(registry_dir, {"id": "0001-valid"})
        register(registry_dir, _SPENT | {"id": "0002-other"})
        _git(tmp_path, "add", "experiments")
        _git(tmp_path, "commit", "-m", "register two records")
        _git(tmp_path, "rm", "-q", "experiments/0002-other.json")
        _git(tmp_path, "commit", "-m", "remove a record")
        result = check_all(repo_root=tmp_path)
        assert result["ok"] is False
        assert any("0002-other: tracked" in error for error in result["errors"])

    def test_a_deleted_record_without_a_result_is_no_loss(self, tmp_path: Path, registry_dir: Path):
        """A record that never ran can be removed before it matters."""
        _init_repo(tmp_path)
        register(registry_dir, {"id": "0001-valid"})
        register(registry_dir, {"id": "0002-draft"})
        _git(tmp_path, "add", "experiments")
        _git(tmp_path, "commit", "-m", "register two records")
        _git(tmp_path, "rm", "-q", "experiments/0002-draft.json")
        _git(tmp_path, "commit", "-m", "drop the draft")
        assert check_all(repo_root=tmp_path)["ok"] is True

    def test_a_renamed_record_fails(self, tmp_path: Path, registry_dir: Path):
        """Renaming a record and resetting its result cannot reset the one-shot rule."""
        _init_repo(tmp_path)
        spent = {
            "id": "0002-a",
            "status": "rejected",
            "result": {
                "games": 20,
                "difference": 0.001,
                "interval": [0.0005, 0.002],
                "csv_path": "experiments/results/0002-a-games.csv",
                "csv_hash": "0" * 64,
            },
        }
        register(registry_dir, spent)
        _git(tmp_path, "add", "experiments")
        _git(tmp_path, "commit", "-m", "register and run")
        _git(tmp_path, "mv", "experiments/0002-a.json", "experiments/0002-b.json")
        make_record({"id": "0002-b"}).save(registry_dir / "0002-b.json")
        _git(tmp_path, "commit", "-am", "rename and reset")
        result = check_all(repo_root=tmp_path)
        assert result["ok"] is False

    def test_a_shallow_clone_fails(self, tmp_path: Path):
        """A shallow clone hides the registration commit, so the check refuses to guess."""
        origin = tmp_path / "origin"
        (origin / "experiments").mkdir(parents=True)
        _init_repo(origin)
        register(origin / "experiments")
        _git(origin, "add", "experiments")
        _git(origin, "commit", "-m", "register the record")
        _git(origin, "commit", "--allow-empty", "-m", "a second commit")
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "--depth", "1", origin.as_uri(), str(clone))
        result = check_all(repo_root=clone)
        assert result["ok"] is False
        assert "shallow clone" in result["errors"][0]

    def test_a_malformed_historical_blob_is_an_error_not_a_crash(
        self, tmp_path: Path, registry_dir: Path
    ):
        """A commit that stored broken JSON for the record names the commit."""
        _init_repo(tmp_path)
        path = registry_dir / "0001-valid.json"
        path.write_text("[1, 2]", encoding="utf-8")
        _git(tmp_path, "add", "experiments")
        _git(tmp_path, "commit", "-m", "store a broken record")
        register(registry_dir)
        result = check_all(repo_root=tmp_path)
        assert result["ok"] is False
        assert "must be a JSON object" in result["errors"][0]


class TestResultPaths:
    """Test that a result names its CSV relative to the repository root."""

    def _result(self, csv_path: str, csv_hash: str) -> dict:
        return {
            "status": "rejected",
            "result": {
                "games": 1,
                "difference": 0.001,
                "interval": [0.0005, 0.002],
                "csv_path": csv_path,
                "csv_hash": csv_hash,
            },
        }

    def test_an_absolute_path_is_refused(self, registry_dir: Path):
        """An absolute path would make the check depend on one machine."""
        outside = registry_dir.parent / "elsewhere.csv"
        outside.write_text("x\n", encoding="utf-8")
        digest = hashlib.sha256(b"x\n").hexdigest()
        register(registry_dir, self._result(str(outside), digest))
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "is not inside experiments/results/" in result["errors"][0]

    def test_a_parent_path_is_refused(self, registry_dir: Path):
        """A path with .. cannot point the hash check at a file outside the results directory."""
        outside = registry_dir.parent / "elsewhere.csv"
        outside.write_text("x\n", encoding="utf-8")
        digest = hashlib.sha256(b"x\n").hexdigest()
        register(registry_dir, self._result("experiments/results/../../elsewhere.csv", digest))
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "is not inside experiments/results/" in result["errors"][0]

    def test_the_path_resolves_against_the_repository_root(
        self, registry_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """The check works from any working directory."""
        csv_path = registry_dir / "results" / "0001-valid-games.csv"
        csv_path.parent.mkdir(parents=True)
        csv_path.write_text("x\n", encoding="utf-8")
        digest = hashlib.sha256(b"x\n").hexdigest()
        register(registry_dir, self._result("experiments/results/0001-valid-games.csv", digest))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert check_all(repo_root=registry_dir.parent)["ok"] is True


class TestRunExperiment:
    """Test run_experiment against a small made-up store."""

    def test_confirmation_run_scores_the_cohort_and_writes_a_result(
        self, registry_dir: Path, store
    ):
        """A confirmation run scores only cohort games and saves status, result, and CSV."""
        seed(store, FIT_SEASONS)
        seed(store, [20262027], first_id=1000, games=ENOUGH_GAMES)
        record = register(registry_dir, {"id": "0002-forward"})
        payload, entries = run_experiment(record.id, store, repo_root=registry_dir.parent)
        assert payload["ok"] is True
        assert payload["scored"] == "seasons from 20262027 onward"
        assert payload["status"] in {"promoted", "rejected", "inconclusive"}
        assert len(entries) == 3 * ENOUGH_GAMES

        saved = ExperimentRecord.load(registry_dir / "0002-forward.json")
        assert saved.status != "registered"
        assert saved.result is not None
        assert saved.result.games == ENOUGH_GAMES
        assert saved.result.interval[0] <= saved.result.difference <= saved.result.interval[1]
        csv_path = registry_dir / "results" / "0002-forward-games.csv"
        assert csv_path.exists()
        assert saved.result.csv_hash == hashlib.sha256(csv_path.read_bytes()).hexdigest()
        checked = check_all(repo_root=registry_dir.parent)
        assert checked["ok"] is True
        assert checked["records"][0]["has_result"] is True

    def test_run_experiment_writes_the_named_games_file(self, registry_dir: Path, store):
        """A run writes the per-game file the caller names, one row per exported game."""
        seed(store, FIT_SEASONS)
        seed(store, [20262027], first_id=1000, games=ENOUGH_GAMES)
        record = register(registry_dir, {"id": "0009-games"})
        out = registry_dir / "scratch-games.csv"
        payload, entries = run_experiment(
            record.id, store, games_out=out, repo_root=registry_dir.parent
        )
        assert payload["games_out"] == str(out)
        assert out.read_text(encoding="utf-8").count("\n") == len(entries) + 1

    def test_confirmation_run_refuses_when_already_run(self, registry_dir: Path, store):
        """The second confirmation run of a record is refused."""
        seed(store, FIT_SEASONS)
        seed(store, [20262027], first_id=1000, games=ENOUGH_GAMES)
        record = register(registry_dir, {"id": "0002-forward"})
        run_experiment(record.id, store, repo_root=registry_dir.parent)
        with pytest.raises(HockeyModelError, match="expected registered"):
            run_experiment(record.id, store, repo_root=registry_dir.parent)

    def test_confirmation_run_refuses_when_a_result_is_already_recorded(
        self, registry_dir: Path, store
    ):
        """A registered record that already holds a result cannot run again."""
        seed(store, FIT_SEASONS)
        seed(store, [20262027], first_id=1000)
        register(
            registry_dir,
            {
                "id": "0008-spent",
                "result": {
                    "games": 20,
                    "difference": -0.001,
                    "interval": [-0.002, 0.000],
                    "csv_path": "experiments/results/0008-spent-games.csv",
                    "csv_hash": "0" * 64,
                },
            },
        )
        with pytest.raises(HockeyModelError, match="already has a result"):
            run_experiment("0008-spent", store, repo_root=registry_dir.parent)

    def test_confirmation_run_refuses_when_not_registered(self, registry_dir: Path, store):
        """A confirmation record with a status other than registered cannot run."""
        seed(store, FIT_SEASONS)
        seed(store, [20262027], first_id=1000)
        record = register(registry_dir, {"id": "0003-spent", "status": "promoted"})
        with pytest.raises(HockeyModelError, match="confirmation status is 'promoted'"):
            run_experiment(record.id, store, repo_root=registry_dir.parent)

    def test_confirmation_run_refuses_when_cohort_has_no_games(self, registry_dir: Path, store):
        """A store with only exploration seasons cannot feed a confirmation cohort."""
        seed(store, FIT_SEASONS)
        record = register(registry_dir, {"id": "0004-early"})
        with pytest.raises(HockeyModelError, match="confirmation cohort has 0 games"):
            run_experiment(record.id, store, repo_root=registry_dir.parent)

    def test_confirmation_run_excludes_playoff_games(self, registry_dir: Path, store):
        """Playoff games in the confirmation season are not part of the cohort."""
        seed(store, FIT_SEASONS)
        seed(store, [20262027], game_type=3, first_id=1000)
        seed(store, [20262027], game_type=2, first_id=2000)
        record = register(registry_dir, {"id": "0005-playoffs", "min_games": 25})
        with pytest.raises(HockeyModelError, match="cohort has 20 games"):
            run_experiment(record.id, store, repo_root=registry_dir.parent)

    def test_confirmation_run_scores_only_games_after_the_cutoff(self, registry_dir: Path, store):
        """Games that started before the registered data_cutoff are not scored."""
        seed(store, FIT_SEASONS)
        seed(store, [20262027], first_id=1000, games=ENOUGH_GAMES)
        for day in range(1, 11):
            add_game(store, 3000 + day, f"2026-10-{day:02d}T00:00", season=20262027)
        record = register(registry_dir, {"id": "0006-cutoff", "min_games": 15})
        _, entries = run_experiment(record.id, store, repo_root=registry_dir.parent)
        scored_ids = {entry["game_id"] for entry in entries}
        assert scored_ids == set(range(1000, 1000 + ENOUGH_GAMES))

    def test_exploration_run_never_writes_a_result(self, registry_dir: Path, store):
        """An exploration run reports its seasons and says it cannot promote anything."""
        seed(store, FIT_SEASONS)
        record = register(registry_dir, {"id": "0007-explore", "cohort": "exploration"})
        payload, _ = run_experiment(record.id, store, repo_root=registry_dir.parent)
        assert payload["ok"] is True
        assert payload["scored"] == "seasons 20162017 to 20252026"
        assert any("exploratory" in note for note in payload["notes"])
        saved = ExperimentRecord.load(registry_dir / "0007-explore.json")
        assert saved.status == "registered"
        assert saved.result is None

    def test_the_stored_result_path_is_relative(self, registry_dir: Path, store):
        """The committed record holds no machine-local path."""
        seed(store, FIT_SEASONS)
        seed(store, [20262027], first_id=1000, games=ENOUGH_GAMES)
        record = register(registry_dir, {"id": "0002-forward"})
        run_experiment(record.id, store, repo_root=registry_dir.parent)
        saved = ExperimentRecord.load(registry_dir / "0002-forward.json")
        assert saved.result is not None
        assert saved.result.csv_path == "experiments/results/0002-forward-games.csv"

    def test_a_failed_export_leaves_the_look_unspent(self, registry_dir: Path, store):
        """If the caller's export cannot be written, the record is not changed."""
        seed(store, FIT_SEASONS)
        seed(store, [20262027], first_id=1000, games=ENOUGH_GAMES)
        record = register(registry_dir, {"id": "0002-forward"})
        bad_out = registry_dir.parent / "missing-dir" / "games.csv"
        with pytest.raises(OSError):
            run_experiment(record.id, store, games_out=bad_out, repo_root=registry_dir.parent)
        saved = ExperimentRecord.load(registry_dir / "0002-forward.json")
        assert saved.status == "registered"
        assert saved.result is None

    def test_a_run_without_an_interval_refuses_to_record(self, registry_dir: Path):
        """A run that scored nothing does not spend the look."""
        record = make_record()
        csv_path = registry_dir / "results" / "0001-valid-games.csv"
        csv_path.parent.mkdir()
        csv_path.write_text("x\n", encoding="utf-8")
        empty_report = BacktestReport(
            candidate_features=(),
            vetoed=(),
            tested_features=(),
            live_features=(),
            validation={},
            runs=(),
            success=False,
            notes=(),
            credits=(),
            missing_inputs={},
        )
        with pytest.raises(HockeyModelError, match="produced no interval"):
            record_with_result(record, empty_report, csv_path, registry_dir.parent)

    def test_the_refusal_names_why_the_run_has_no_interval(self, registry_dir: Path):
        record = make_record()
        csv_path = registry_dir / "results" / "0001-valid-games.csv"
        csv_path.parent.mkdir()
        csv_path.write_text("x\n", encoding="utf-8")
        report = BacktestReport(
            (),
            (),
            (),
            (),
            {},
            (),
            False,
            (),
            (),
            {},
            eligible=False,
            ineligible_reasons=("season 20262027 has 3 scored regular season games",),
        )
        with pytest.raises(HockeyModelError, match="season 20262027 has 3 scored"):
            record_with_result(record, report, csv_path, registry_dir.parent)


class TestMain:
    """Test the CLI entry point."""

    def test_main_exits_zero_on_valid_records(
        self, registry_dir: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """main reads the registry of the working directory and exits zero when valid."""
        exited: list[int] = []

        def fake_exit(code: int | None = None) -> None:
            exited.append(code if code is not None else 0)
            raise SystemExit(code)

        register(registry_dir)
        monkeypatch.chdir(repo_root)
        monkeypatch.setattr("sys.exit", fake_exit)
        from hockey_model.experiments.check import main

        try:
            main()
        except SystemExit:
            pass
        assert exited == [0]
