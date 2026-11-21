"""Tests for scripts/mutation_timeouts.py, the list of timed-out mutants (#274)."""

import importlib.util
import json
import math
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path
from mutmut.mutation.data import SourceFileMutationData
from mutmut.stats import status_by_exit_code

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "mutation_timeouts.py")


def _load() -> ModuleType:
    """Import the script by path, because scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("mutation_timeouts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


timeouts = _load()


def _load_shards() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "mutation_shards", require_path(ROOT / "scripts" / "mutation_shards.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shards = _load_shards()


def _meta(path: Path, codes: dict[str, int | None], estimated: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"exit_code_by_key": codes, "estimated_durations_by_key": estimated}),
        encoding="utf-8",
    )


def _pyproject(path: Path, body: str) -> Path:
    path.write_text(f"[tool.mutmut]\n{body}\n", encoding="utf-8")
    return path


def test_the_timeout_exit_codes_are_the_ones_mutmut_names_a_timeout() -> None:
    named = {code for code, status in status_by_exit_code.items() if status == "timeout"}
    assert set(timeouts.TIMEOUT_EXIT_CODES) == named


@pytest.mark.parametrize("code", sorted(timeouts.TIMEOUT_EXIT_CODES))
def test_a_timeout_exit_code_is_listed_with_its_limit(tmp_path: Path, code: int) -> None:
    _meta(tmp_path / "src" / "a.py.meta", {"m.x_f__mutmut_1": code}, {"m.x_f__mutmut_1": 3.2})
    found = timeouts.collect(tmp_path, 3.0, 1.0)
    assert found == [{"mutant": "m.x_f__mutmut_1", "estimated_s": 3.2, "limit_s": 26}]


@pytest.mark.parametrize("code", [0, 1, 2, 3, 5, 33, 34, 35, 37, -9, -11, None])
def test_an_outcome_that_is_not_a_timeout_is_left_out(tmp_path: Path, code: int | None) -> None:
    _meta(tmp_path / "a.py.meta", {"m.x_f__mutmut_1": code}, {"m.x_f__mutmut_1": 3.2})
    assert timeouts.collect(tmp_path, 3.0, 1.0) == []


def test_the_list_spans_every_meta_file_and_is_sorted_by_name(tmp_path: Path) -> None:
    _meta(tmp_path / "src" / "b.py.meta", {"b.x_g__mutmut_2": 36}, {"b.x_g__mutmut_2": 1.0})
    _meta(
        tmp_path / "src" / "deep" / "a.py.meta", {"a.x_f__mutmut_1": 36}, {"a.x_f__mutmut_1": 2.0}
    )
    names = [item["mutant"] for item in timeouts.collect(tmp_path, 3.0, 1.0)]
    assert names == ["a.x_f__mutmut_1", "b.x_g__mutmut_2"]


def test_a_timed_out_mutant_without_an_estimate_is_an_error(tmp_path: Path) -> None:
    _meta(tmp_path / "a.py.meta", {"m.x_f__mutmut_1": 36}, {})
    with pytest.raises(timeouts.InputError, match="no estimated time"):
        timeouts.collect(tmp_path, 3.0, 1.0)


def test_a_missing_directory_and_a_bad_meta_file_are_errors(tmp_path: Path) -> None:
    with pytest.raises(timeouts.InputError, match="is not a directory"):
        timeouts.collect(tmp_path / "none", 3.0, 1.0)
    (tmp_path / "bad.py.meta").write_text("not json", encoding="utf-8")
    with pytest.raises(timeouts.InputError, match="cannot read the mutmut meta file"):
        timeouts.collect(tmp_path, 3.0, 1.0)


def test_the_limits_come_from_the_mutmut_table_or_the_mutmut_defaults(tmp_path: Path) -> None:
    both = _pyproject(tmp_path / "a.toml", "timeout_multiplier = 3.0\ntimeout_constant = 2")
    assert timeouts.read_limits(both) == (3.0, 2.0)
    assert timeouts.read_limits(_pyproject(tmp_path / "b.toml", "")) == (15.0, 1.0)
    empty = tmp_path / "c.toml"
    empty.write_text("", encoding="utf-8")
    assert timeouts.read_limits(empty) == (15.0, 1.0)


@pytest.mark.parametrize("body", ["timeout_multiplier = -1", 'timeout_multiplier = "3"'])
def test_a_limit_that_is_not_a_number_is_an_error(tmp_path: Path, body: str) -> None:
    with pytest.raises(timeouts.InputError, match="timeout_multiplier"):
        timeouts.read_limits(_pyproject(tmp_path / "a.toml", body))


@given(
    estimated=st.floats(min_value=0.0, max_value=5000.0),
    extra=st.floats(min_value=0.0, max_value=100.0),
    multiplier=st.floats(min_value=1.0, max_value=20.0),
)
def test_the_limit_is_the_ceiling_of_the_mutmut_formula_and_grows_with_the_estimate(
    estimated: float, extra: float, multiplier: float
) -> None:
    limit = timeouts.cpu_limit(estimated, multiplier, 1.0)
    assert limit == math.ceil((estimated + 1.0) * multiplier * 2)
    assert limit >= 2 * multiplier
    assert timeouts.cpu_limit(estimated + extra, multiplier, 1.0) >= limit


def test_the_command_writes_the_list_and_names_the_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _meta(tmp_path / "m" / "a.py.meta", {"m.x_f__mutmut_1": 36}, {"m.x_f__mutmut_1": 3.2})
    pyproject = _pyproject(tmp_path / "p.toml", "timeout_multiplier = 3.0")
    out = tmp_path / "out.json"
    argv = ["--mutants", str(tmp_path / "m"), "--pyproject", str(pyproject), "--out", str(out)]
    assert timeouts.main(argv) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["timeout_multiplier"] == 3.0 and data["timeout_constant"] == 1.0
    assert data["timeouts"] == [{"mutant": "m.x_f__mutmut_1", "estimated_s": 3.2, "limit_s": 26}]
    assert "1 timed-out mutant" in capsys.readouterr().err


def test_the_command_fails_with_the_reason_when_the_input_is_bad(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--mutants", str(tmp_path / "none"), "--out", str(tmp_path / "out.json")]
    assert timeouts.main(argv) == 1
    assert "mutation-timeouts:" in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("owner_first", [True, False])
def test_a_timeout_keeps_its_estimate_through_the_shard_merge(
    tmp_path: Path, owner_first: bool
) -> None:
    """A shard sets the estimate only for the mutants it ran, so the merge must join them."""
    owner, other = tmp_path / "owner", tmp_path / "other"
    key = "m.x_f__mutmut_1"
    hashes = {"hash_by_function_name": {"f": "abc"}}
    for directory, code, estimate in ((owner, 36, {key: 2.5}), (other, None, {})):
        path = directory / "src" / "a.py.meta"
        path.parent.mkdir(parents=True)
        body = {"exit_code_by_key": {key: code}, "estimated_durations_by_key": estimate, **hashes}
        path.write_text(json.dumps(body), encoding="utf-8")
    merged = tmp_path / "merged"
    shards.merge_meta([owner, other] if owner_first else [other, owner], merged)
    assert timeouts.collect(merged, 3.0, 1.0) == [
        {"mutant": key, "estimated_s": 2.5, "limit_s": 21}
    ]


def test_the_list_reads_the_meta_file_that_mutmut_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mutmut's own writer names the fields that the reader uses."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mutants" / "src").mkdir(parents=True)
    data = SourceFileMutationData(path="src/a.py")
    data.exit_code_by_key = {"m.x_f__mutmut_1": 36, "m.x_f__mutmut_2": 1}
    data.estimated_time_of_tests_by_mutant = {"m.x_f__mutmut_1": 4.0, "m.x_f__mutmut_2": 4.0}
    data.save()
    assert timeouts.collect(tmp_path / "mutants", 3.0, 1.0) == [
        {"mutant": "m.x_f__mutmut_1", "estimated_s": 4.0, "limit_s": 30}
    ]
