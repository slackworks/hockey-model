"""A deleted confirmation record that never ran must not free its number (#291)."""

import subprocess
from pathlib import Path

import pytest
from experiment_support import register
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.experiments.check import check_all
from hockey_model.experiments.history import deleted_record_ids, outlived, parse_deletion_log
from hockey_model.types import HockeyModelError

_SPENT = {
    "status": "rejected",
    "result": {
        "games": 20,
        "difference": 0.001,
        "interval": [0.0005, 0.002],
        "csv_path": "experiments/results/0002-ran-games.csv",
        "csv_hash": "0" * 64,
    },
}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path, registry_dir: Path) -> Path:
    """Return a repository with an identity and no signing, so commits work anywhere."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.com")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A", "experiments")
    _git(repo, "commit", "-q", "-m", message)


def _remove(repo: Path, name: str) -> None:
    _git(repo, "rm", "-q", f"experiments/{name}.json")
    _git(repo, "commit", "-q", "-m", f"remove {name}")


def _register_two(repo: Path, registry_dir: Path) -> None:
    """Register 0001, which never runs, and 0002, which holds a result."""
    register(registry_dir, {"id": "0001-unrun", "look_number": 1})
    register(registry_dir, _SPENT | {"id": "0002-ran", "look_number": 2})
    _commit(repo, "register two records")


def test_the_scenario_of_the_issue_stays_red_after_the_number_is_reused(
    repo: Path, registry_dir: Path
) -> None:
    _register_two(repo, registry_dir)
    _remove(repo, "0001-unrun")
    register(registry_dir, {"id": "0001-other", "look_number": 1})
    result = check_all(repo_root=repo)
    assert result["ok"] is False
    assert any(error.startswith("0001-unrun: tracked") for error in result["errors"])


def test_an_unrun_record_below_a_higher_confirmation_record_cannot_be_deleted(
    repo: Path, registry_dir: Path
) -> None:
    _register_two(repo, registry_dir)
    _remove(repo, "0001-unrun")
    assert deleted_record_ids(["0002-ran"], repo) == ["0001-unrun"]


def test_the_error_names_the_withdrawn_status_as_the_way_to_drop_a_record(
    repo: Path, registry_dir: Path
) -> None:
    _register_two(repo, registry_dir)
    _remove(repo, "0001-unrun")
    [error] = [e for e in check_all(repo_root=repo)["errors"] if e.startswith("0001-unrun")]
    assert "withdrawn" in error


def test_a_deletion_before_any_higher_record_existed_stays_allowed(
    repo: Path, registry_dir: Path
) -> None:
    """The real history holds this: 0001-test was deleted, and a higher record came later."""
    register(registry_dir, {"id": "0001-test", "look_number": 1})
    _commit(repo, "register the first record")
    _remove(repo, "0001-test")
    register(registry_dir, {"id": "0001-v1", "look_number": 1})
    register(registry_dir, _SPENT | {"id": "0002-ran", "look_number": 2})
    _commit(repo, "register later records")
    assert deleted_record_ids(["0001-v1", "0002-ran"], repo) == []


def test_the_highest_unrun_record_can_be_deleted(repo: Path, registry_dir: Path) -> None:
    register(registry_dir, {"id": "0001-first", "look_number": 1})
    register(registry_dir, {"id": "0002-draft", "look_number": 2})
    _commit(repo, "register two records")
    _remove(repo, "0002-draft")
    assert deleted_record_ids(["0001-first"], repo) == []


def test_an_exploration_record_does_not_hold_a_number(repo: Path, registry_dir: Path) -> None:
    register(registry_dir, {"id": "0001-unrun", "look_number": 1})
    register(registry_dir, {"id": "0002-explore", "cohort": "exploration", "look_number": None})
    _commit(repo, "register a confirmation and an exploration record")
    _remove(repo, "0001-unrun")
    assert deleted_record_ids(["0002-explore"], repo) == []


def test_a_deleted_exploration_record_is_no_loss(repo: Path, registry_dir: Path) -> None:
    register(registry_dir, {"id": "0001-explore", "cohort": "exploration", "look_number": None})
    register(registry_dir, {"id": "0002-ran", "look_number": 1} | _SPENT)
    _commit(repo, "register two records")
    _remove(repo, "0001-explore")
    assert deleted_record_ids(["0002-ran"], repo) == []


def test_a_name_that_is_not_a_record_id_is_not_read(repo: Path, registry_dir: Path) -> None:
    (registry_dir / "notes.json").write_text("{}\n", encoding="utf-8")
    register(registry_dir, {"id": "0002-ran", "look_number": 1} | _SPENT)
    _commit(repo, "add a stray file")
    _remove(repo, "notes")
    assert deleted_record_ids(["0002-ran"], repo) == []


def test_the_registry_of_this_repository_passes() -> None:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        pytest.skip("the mutation copy has no git history")
    assert check_all(repo_root=root)["ok"] is True


def test_two_records_deleted_in_one_commit_are_both_flagged(repo: Path, registry_dir: Path) -> None:
    register(registry_dir, {"id": "0001-first", "look_number": 1})
    register(registry_dir, {"id": "0002-second", "look_number": 2})
    register(registry_dir, _SPENT | {"id": "0003-ran", "look_number": 3})
    _commit(repo, "register three records")
    _git(repo, "rm", "-q", "experiments/0001-first.json", "experiments/0002-second.json")
    _git(repo, "commit", "-q", "-m", "remove two records")
    assert deleted_record_ids(["0003-ran"], repo) == ["0001-first", "0002-second"]


def test_a_record_that_came_back_is_not_flagged_and_its_second_deletion_is(
    repo: Path, registry_dir: Path
) -> None:
    register(registry_dir, {"id": "0001-unrun", "look_number": 1})
    _commit(repo, "register the first record")
    _remove(repo, "0001-unrun")
    register(registry_dir, {"id": "0001-unrun", "look_number": 1})
    register(registry_dir, _SPENT | {"id": "0002-ran", "look_number": 2})
    _commit(repo, "the record comes back, and a higher one is registered")
    assert deleted_record_ids(["0001-unrun", "0002-ran"], repo) == []
    _remove(repo, "0001-unrun")
    assert deleted_record_ids(["0002-ran"], repo) == ["0001-unrun"]


_HEX = st.text(alphabet="0123456789abcdef", min_size=40, max_size=40)
_IDS = st.from_regex(r"[0-9]{4}-[a-z][a-z0-9]{0,8}", fullmatch=True)


@settings(max_examples=60, deadline=None)
@given(
    commits=st.lists(
        st.tuples(_HEX, st.lists(st.tuples(_IDS, st.sampled_from([".json", ".csv"])), max_size=4)),
        max_size=5,
    )
)
def test_the_deletion_log_gives_back_the_json_files_that_it_was_built_from(
    commits: list[tuple[str, list[tuple[str, str]]]],
) -> None:
    text = ""
    for sha, files in commits:
        text += f"commit:{sha}\n\n" + "".join(f"experiments/{n}{ext}\n" for n, ext in files)
    expected = [(sha, n) for sha, files in commits for n, ext in files if ext == ".json"]
    assert parse_deletion_log(text) == expected


@settings(max_examples=100, deadline=None)
@given(text=st.text(max_size=200))
def test_the_deletion_log_parser_returns_pairs_or_raises_the_package_error(text: str) -> None:
    try:
        pairs = parse_deletion_log(text)
    except HockeyModelError:
        return
    assert all(isinstance(commit, str) and isinstance(name, str) for commit, name in pairs)


def test_a_file_name_before_any_commit_is_refused() -> None:
    with pytest.raises(HockeyModelError, match="before any commit"):
        parse_deletion_log("experiments/0001-a.json\n")


@settings(max_examples=100, deadline=None)
@given(
    deleted=st.integers(0, 9998),
    others=st.lists(st.tuples(st.integers(0, 9999), st.booleans()), max_size=8),
)
def test_a_record_is_outlived_exactly_when_a_higher_number_is_a_confirmation(
    deleted: int, others: list[tuple[int, bool]]
) -> None:
    kinds = {f"{number:04d}-x": confirmation for number, confirmation in others}
    expected = any(
        number > deleted and confirmation
        for number, confirmation in ((int(i[:4]), c) for i, c in kinds.items())
    )
    ids = list(kinds)
    assert outlived(f"{deleted:04d}-d", ids, kinds.__getitem__) is expected
    assert outlived(f"{deleted:04d}-d", reversed(ids), kinds.__getitem__) is expected


def test_a_name_that_is_not_a_record_id_never_outlives_a_record() -> None:
    assert outlived("0001-a", ["9999", "notes", "0002_a"], lambda _: True) is False


def test_a_ran_record_renamed_with_an_edit_is_a_deleted_record(
    repo: Path, registry_dir: Path
) -> None:
    """Git pairs a removed file with an added file by likeness, so a rename must not matter."""
    register(registry_dir, _SPENT | {"id": "0002-ran", "look_number": 1})
    _commit(repo, "register and run")
    _git(repo, "mv", "experiments/0002-ran.json", "experiments/0002-moved.json")
    register(registry_dir, _SPENT | {"id": "0002-moved", "title": "Another title"})
    _commit(repo, "move it")
    result = check_all(repo_root=repo)
    assert result["ok"] is False
    assert any("0002-ran: tracked" in error for error in result["errors"])


def test_a_new_record_in_the_commit_that_deletes_an_unrun_record_is_no_loss(
    repo: Path, registry_dir: Path
) -> None:
    """The new record did not outlive the deleted one, so a squash merge keeps the verdict."""
    register(registry_dir, {"id": "0002-unrun", "look_number": 1})
    _commit(repo, "register one record")
    _git(repo, "rm", "-q", "experiments/0002-unrun.json")
    register(registry_dir, {"id": "0003-new", "look_number": 1, "title": "Another title"})
    _commit(repo, "replace it")
    assert check_all(repo_root=repo)["ok"] is True
