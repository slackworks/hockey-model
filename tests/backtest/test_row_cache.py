"""The season row files: a complete file or none, a bad file is a miss, a failed write warns."""

import os
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from test_runner_store import PLAN, seed

from hockey_model.backtest.row_cache import KEEP_FEATURE_LISTS, SeasonFile, cache_dir
from hockey_model.backtest.row_codec import encode_rows
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


def test_a_temporary_file_of_a_writer_at_work_is_not_removed(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    file = _file(tmp_path)
    live = tmp_path / f".{file.path.name}.running.tmp"
    live.write_text("[")
    file.save(rows)
    assert live.exists()


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
    assert f"game {first.game.game_id}: elo_logit is nan" in err
    assert "writable" not in err
    assert not directory.exists()


def test_a_game_with_a_naive_start_is_a_warning_and_not_a_crash(
    tmp_path: Path, rows: list[TrainingRow], capsys: pytest.CaptureFixture[str]
) -> None:
    first = rows[0]
    naive = replace(first.game, start=datetime(2020, 1, 1))  # noqa: DTZ001 the case under test
    _file(tmp_path / "cache").save([replace(first, game=naive)])
    assert "cannot encode the rows" in capsys.readouterr().err


def _aged(file: SeasonFile, age: int) -> None:
    """Set the file time ``age`` seconds before a fixed moment, so the order is exact."""
    moment = 1_700_000_000 - age
    os.utime(file.path, (moment, moment))


def _lists(directory: Path, count: int, rows: list[TrainingRow]) -> list[SeasonFile]:
    """Put ``count`` feature lists in the directory, the first one the oldest.

    The files are written without a save, so no save removes one while the test sets them up.
    """
    directory.mkdir(exist_ok=True)
    files = [_file(directory, names=(f"list{i}",)) for i in range(count)]
    for age, file in zip(range(count, 0, -1), files, strict=True):
        file.path.write_text(encode_rows(rows))
        _aged(file, age)
    return files


def test_the_files_of_the_newest_feature_lists_stay_and_the_older_ones_go(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    files = _lists(tmp_path, KEEP_FEATURE_LISTS + 2, rows)
    newcomer = _file(tmp_path, names=("newcomer",))
    newcomer.save(rows)
    survivors = [f for f in [*files, newcomer] if f.path.exists()]
    assert survivors == [*files[-(KEEP_FEATURE_LISTS - 1) :], newcomer]


def test_a_feature_list_that_was_read_stays_over_one_that_was_not(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    files = _lists(tmp_path, KEEP_FEATURE_LISTS, rows)
    oldest = files[0]
    assert oldest.load(_ids(rows)) == rows
    _file(tmp_path, names=("newcomer",)).save(rows)
    assert oldest.path.exists()
    assert not files[1].path.exists()


def test_another_season_or_mode_keeps_all_of_its_feature_lists(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    others = [replace(f, season=4) for f in _lists(tmp_path, KEEP_FEATURE_LISTS + 2, rows)]
    others.append(replace(_file(tmp_path, names=("g",)), mode=GoalieMode.GUESS))
    for other in others:
        other.path.write_text(encode_rows(rows))
    _file(tmp_path, names=("newcomer",)).save(rows)
    assert all(other.path.exists() for other in others)


def test_a_file_that_cannot_be_removed_is_a_warning_and_the_save_stands(
    tmp_path: Path,
    rows: list[TrainingRow],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lists(tmp_path, KEEP_FEATURE_LISTS, rows)

    def refuse(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(13, "no", str(self))

    monkeypatch.setattr(Path, "unlink", refuse)
    newcomer = _file(tmp_path, names=("newcomer",))
    newcomer.save(rows)
    assert "cannot remove the old cached rows" in capsys.readouterr().err
    assert newcomer.path.exists()


def test_a_hit_on_a_file_that_cannot_be_marked_is_still_a_hit_with_a_warning(
    tmp_path: Path,
    rows: list[TrainingRow],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = _file(tmp_path)
    file.save(rows)

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "no")

    monkeypatch.setattr(os, "utime", refuse)
    assert file.load(_ids(rows)) == rows
    assert "as used" in capsys.readouterr().err


def test_the_list_that_was_just_saved_stays_even_when_other_files_look_newer(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    files = _lists(tmp_path, KEEP_FEATURE_LISTS, rows)
    for file in files:
        # A file time in the future, as a clock that moved back would leave.
        os.utime(file.path, (4_000_000_000, 4_000_000_000))
    newcomer = _file(tmp_path, names=("newcomer",))
    newcomer.save(rows)
    assert newcomer.path.exists()


def test_a_link_to_nothing_among_the_files_is_removed_and_does_not_stop_the_save(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    files = _lists(tmp_path, KEEP_FEATURE_LISTS, rows)
    dangling = tmp_path / f"actual-3-{'f' * 12}-{OTHER}.json"
    dangling.symlink_to(tmp_path / "missing.json")
    newcomer = _file(tmp_path, names=("newcomer",))
    newcomer.save(rows)
    assert newcomer.path.exists()
    # The link ranks as a list of its own, so the two oldest real lists go.
    assert [f.path.exists() for f in files] == [False, False] + [True] * (len(files) - 2)


def test_lists_with_the_same_file_time_are_ranked_by_tag_and_not_by_the_listing(
    tmp_path: Path, rows: list[TrainingRow]
) -> None:
    files = _lists(tmp_path, KEEP_FEATURE_LISTS + 1, rows)
    for file in files:
        os.utime(file.path, (1_700_000_000, 1_700_000_000))
    newcomer = _file(tmp_path, names=("newcomer",))
    newcomer.save(rows)
    by_tag = sorted(files, key=lambda f: f.path.name.split("-")[2])
    assert [f.path.exists() for f in by_tag] == [True] * (KEEP_FEATURE_LISTS - 1) + [False] * 2


# A file time comes from the filesystem, so the test sets each file to a whole second instead.
_MOMENT = 1_700_000_000
_MAX_LISTS = 12
# A drawn list of (age in seconds, list name) pairs, with no name twice and many ages equal.
_ENTRIES = st.lists(
    st.tuples(st.integers(min_value=0, max_value=1000), st.integers(min_value=0, max_value=9999)),
    min_size=1,
    max_size=_MAX_LISTS,
    unique_by=lambda pair: pair[1],
)


@settings(max_examples=25, deadline=None)
@given(entries=_ENTRIES)
def test_a_save_keeps_the_newest_feature_lists_and_the_one_it_saved(
    tmp_path_factory: pytest.TempPathFactory, entries: list[tuple[int, str]]
) -> None:
    """The retention rule: the saved list stays, and the rest rank by file time, then tag."""
    directory = tmp_path_factory.mktemp("retention")
    files = [_file(directory, names=(f"list{name}",)) for _, name in entries]
    stamps: dict[SeasonFile, int] = {}
    for (age, _), file in zip(entries, files, strict=True):
        file.path.write_text("")
        stamps[file] = _MOMENT - age
        os.utime(file.path, (stamps[file], stamps[file]))
    # The saved list holds the oldest file time, so only its own place keeps it.
    newcomer = _file(directory, names=("newcomer",))
    newcomer.path.write_text("")
    stamps[newcomer] = _MOMENT - max(age for age, _ in entries) - 1
    os.utime(newcomer.path, (stamps[newcomer], stamps[newcomer]))
    newcomer._remove_other_lists()
    ranked = sorted([*files, newcomer], key=lambda f: (f is not newcomer, -stamps[f], f._tag))
    survivors = {f._tag for f in [*files, newcomer] if f.path.exists()}
    assert survivors == {f._tag for f in ranked[:KEEP_FEATURE_LISTS]}


def test_a_file_that_another_run_removed_before_the_mark_is_not_a_warning(
    tmp_path: Path,
    rows: list[TrainingRow],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = _file(tmp_path)
    file.save(rows)

    def gone(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "gone")

    monkeypatch.setattr(os, "utime", gone)
    assert file.load(_ids(rows)) == rows
    assert capsys.readouterr().err == ""
