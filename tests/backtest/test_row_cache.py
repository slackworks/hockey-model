"""The season row files: a complete file or none, a bad file is a miss, a failed write warns."""

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from test_runner_store import PLAN, seed

from hockey_model.backtest.row_cache import SeasonFile, cache_dir
from hockey_model.game_model.features import FEATURES, FeatureRow, GoalieMode
from hockey_model.game_model.training import TrainingRow, training_rows

KEY = "a" * 64
OTHER = "b" * 64
NAMES = tuple(FEATURES)


def _file(
    directory: Path,
    mode: GoalieMode = GoalieMode.ACTUAL,
    season: int = 3,
    names: tuple[str, ...] = NAMES,
    key: str = KEY,
) -> SeasonFile:
    return SeasonFile(directory, mode, season, names, key)


def _ids(rows: list[TrainingRow]) -> list[int]:
    return [row.game.game_id for row in rows]


@pytest.fixture
def rows(store: sqlite3.Connection) -> list[TrainingRow]:
    seed(store, range(1, 4))
    store.commit()
    return training_rows(store, 3, 3, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES)


def test_the_directory_is_beside_the_store_file(tmp_path: Path) -> None:
    assert cache_dir(tmp_path / "x" / "store.db") == tmp_path / "x" / "season-rows"


def test_saved_rows_load_back_equal(tmp_path: Path, rows: list[TrainingRow]) -> None:
    _file(tmp_path).save(rows)
    assert rows
    assert _file(tmp_path).load(_ids(rows)) == rows


def test_a_season_with_no_games_is_a_hit_and_not_a_miss(tmp_path: Path) -> None:
    _file(tmp_path, GoalieMode.GUESS).save([])
    assert _file(tmp_path, GoalieMode.GUESS).load([]) == []


@pytest.mark.parametrize(
    "other",
    [
        lambda f: replace(f, mode=GoalieMode.GUESS),
        lambda f: replace(f, season=4),
        lambda f: replace(f, key=OTHER),
        lambda f: replace(f, names=("xg_form", "rest")),
        lambda f: replace(f, names=tuple(reversed(NAMES))),
    ],
    ids=["mode", "season", "key", "features", "feature order"],
)
def test_another_mode_season_key_or_feature_list_is_a_miss(
    tmp_path: Path, rows: list[TrainingRow], other: Callable[[SeasonFile], SeasonFile]
) -> None:
    saved = _file(tmp_path)
    saved.save(rows)
    assert other(saved).load(_ids(rows)) is None


def test_a_missing_directory_is_a_miss_without_a_word(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _file(tmp_path / "none").load([1]) is None
    assert capsys.readouterr().err == ""


def test_a_new_key_removes_the_old_file_of_the_same_mode_season_and_features_only(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    kept = (
        _file(tmp_path, season=4),
        _file(tmp_path, GoalieMode.GUESS),
        _file(tmp_path, names=("xg_form", "rest")),
    )
    _file(tmp_path).save(rows)
    for file in kept:
        file.save(rows)
    _file(tmp_path, key=OTHER).save(rows)
    assert not _file(tmp_path).path.exists()
    assert _file(tmp_path, key=OTHER).load(_ids(rows)) == rows
    assert all(file.path.exists() for file in kept)


def test_no_temporary_file_stays_and_a_stale_one_is_removed(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    directory = tmp_path / "cache"
    directory.mkdir()
    file = _file(directory)
    stale = directory / f".{file.path.name.replace(KEY, OTHER)}.killed.tmp"
    stale.write_text("[")
    file.save(rows)
    assert list(directory.iterdir()) == [file.path]


def test_a_feature_name_does_not_reach_the_file_name(tmp_path: Path) -> None:
    assert ".." not in _file(tmp_path, names=("../../x",)).path.name


@pytest.mark.parametrize("damage", ["", "not json", "[{}]", "[1]", "[" + "9" * 5000 + "]"])
def test_a_damaged_file_is_a_miss_that_names_the_file_and_the_next_save_repairs_it(
    tmp_path: Path,
    rows: list[TrainingRow],
    capsys: pytest.CaptureFixture[str],
    damage: str,
) -> None:
    file = _file(tmp_path)
    file.path.write_text(damage)
    assert file.load(_ids(rows)) is None
    assert str(file.path) in capsys.readouterr().err
    file.save(rows)
    assert file.load(_ids(rows)) == rows


def test_a_file_that_is_not_text_is_a_miss(tmp_path: Path) -> None:
    file = _file(tmp_path)
    file.path.write_bytes(b"\xff\xfe\x00")
    assert file.load([1]) is None


def _fewer(ids: list[int]) -> list[int]:
    return ids[:-1]


def _more(ids: list[int]) -> list[int]:
    return [*ids, 999]


def _reversed(ids: list[int]) -> list[int]:
    return ids[::-1]


def _other(ids: list[int]) -> list[int]:
    return [999, *ids[1:]]


def _none(ids: list[int]) -> list[int]:
    return []


@pytest.mark.parametrize(
    "wanted",
    [_none, _other, _fewer, _more, _reversed],
    ids=["no games", "other game", "fewer", "more", "other order"],
)
def test_a_file_whose_games_differ_from_the_store_is_a_miss_that_says_so(
    tmp_path: Path,
    rows: list[TrainingRow],
    capsys: pytest.CaptureFixture[str],
    wanted: Callable[[list[int]], list[int]],
) -> None:
    file = _file(tmp_path)
    file.save(rows)
    assert file.load(wanted(_ids(rows))) is None
    assert "do not hold the games of season 3" in capsys.readouterr().err


def test_an_empty_file_is_a_miss_when_the_store_has_games(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    file = _file(tmp_path)
    file.save([])
    assert file.load(_ids(rows)) is None


def test_rows_of_another_season_in_a_file_are_a_miss(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    _file(tmp_path, season=4).save(rows)
    assert _file(tmp_path, season=4).load(_ids(rows)) is None


def test_a_row_whose_feature_game_differs_from_its_game_is_a_miss(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    first = rows[0]
    other = replace(first.features, game_id=first.game.game_id + 1000)
    _file(tmp_path).save([replace(first, features=other)])
    assert _file(tmp_path).load([first.game.game_id]) is None


def test_a_link_in_place_of_the_file_is_not_read_and_its_target_is_left_alone(
    tmp_path: Path, rows: list[TrainingRow], capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("[]")
    file = _file(tmp_path / "cache")
    file.directory.mkdir()
    file.path.symlink_to(target)
    assert file.load([]) is None
    assert "are a link" in capsys.readouterr().err
    file.save(rows)
    assert target.read_text() == "[]"
    assert not file.path.is_symlink()
    assert file.load(_ids(rows)) == rows


def test_the_reads_mode_is_refused_because_the_key_does_not_cover_it(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reads mode"):
        _file(tmp_path, GoalieMode.READS)


def test_a_directory_that_cannot_be_made_is_a_warning_and_not_an_error(
    tmp_path: Path, rows: list[TrainingRow], capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "season-rows"
    blocker.write_text("a file where the directory should be")
    _file(blocker).save(rows)
    err = capsys.readouterr().err
    assert "cannot write the cached rows" in err
    assert "writable" in err


def test_a_row_that_cannot_be_encoded_is_a_warning_that_does_not_blame_the_disk(
    tmp_path: Path, rows: list[TrainingRow], capsys: pytest.CaptureFixture[str]
) -> None:
    first = rows[0]
    bad = FeatureRow(
        first.features.game_id,
        float("nan"),
        dict(first.features.values),
        first.features.missing,
        first.features.uses_moneypuck,
        first.features.imputed,
    )
    directory = tmp_path / "cache"
    _file(directory).save([TrainingRow(first.game, bad, True)])
    err = capsys.readouterr().err
    assert "cannot encode the rows" in err
    assert "writable" not in err
    assert not directory.exists()


def test_a_game_with_a_naive_start_is_a_warning_and_not_a_crash(
    tmp_path: Path, rows: list[TrainingRow], capsys: pytest.CaptureFixture[str]
) -> None:
    first = rows[0]
    naive = replace(first.game, start=datetime(2020, 1, 1))  # noqa: DTZ001 the case under test
    _file(tmp_path / "cache").save([replace(first, game=naive)])
    assert "cannot encode the rows" in capsys.readouterr().err
