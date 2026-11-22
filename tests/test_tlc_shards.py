"""Tests for scripts/tlc_shards.py and the shard map specs/shards.json."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import jsonschema
import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "tlc_shards.py")
SHARD_MAP = require_path(ROOT / "specs" / "shards.json")
SCHEMA = require_path(ROOT / "specs" / "shards.schema.json")


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tlc_shards", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shards = _load_module()
DATA = shards.load_map(SHARD_MAP)
NAMES = [shard["name"] for shard in DATA["shards"]]


def _run(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-I", str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_the_map_validates_against_its_schema() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(json.loads(SHARD_MAP.read_text(encoding="utf-8")), schema)


def test_every_shard_names_files_that_exist() -> None:
    for shard in DATA["shards"]:
        assert (ROOT / shard["spec"]).is_file(), shard
        assert (ROOT / shard["config"]).is_file(), shard


def test_every_config_in_specs_belongs_to_one_shard() -> None:
    configs = sorted(shard["config"] for shard in DATA["shards"])
    assert configs == sorted(f"specs/{path.name}" for path in (ROOT / "specs").glob("*.cfg"))


def test_every_covered_path_exists() -> None:
    for path in DATA["covers"]:
        assert (ROOT / path).is_file(), f"{path} is in covers of specs/shards.json but is gone"


def test_a_change_of_the_fit_command_selects_no_shard() -> None:
    assert shards.plan(DATA, ["src/hockey_model/store_cli.py"]) == []
    assert shards.plan(DATA, ["src/hockey_model/store_cli_parsers.py"]) == []


def test_a_change_of_the_refresh_command_selects_every_shard() -> None:
    assert shards.plan(DATA, ["src/hockey_model/store_cli_refresh.py"]) == NAMES


@pytest.mark.parametrize(
    "path", ["specs/daily_prepare.tla", "scripts/tlc.sh", ".github/workflows/tla.yml"]
)
def test_a_change_of_an_always_path_selects_every_shard(path: str) -> None:
    assert shards.plan(DATA, [path, "README.md"]) == NAMES


def test_an_unknown_path_selects_no_shard() -> None:
    assert shards.plan(DATA, ["README.md", "docs/x.md"]) == []
    assert shards.plan(DATA, []) == []


def test_a_path_that_two_shards_cover_selects_both() -> None:
    data = {
        "always": ["a/**"],
        "covers": [],
        "shards": [
            {"name": "one", "spec": "s", "config": "c1", "paths": ["x.py"]},
            {"name": "two", "spec": "s", "config": "c2", "paths": ["x.py", "y.py"]},
            {"name": "three", "spec": "s", "config": "c3"},
        ],
    }
    assert shards.plan(data, ["x.py"]) == ["one", "two"]
    assert shards.plan(data, ["y.py"]) == ["two"]


@given(path=st.text(alphabet="abc/.", max_size=20))
def test_a_directory_pattern_matches_exactly_the_paths_under_it(path: str) -> None:
    assert shards.matches(path, ["abc/**"]) == path.startswith("abc/")


def test_list_prints_one_json_object_per_shard() -> None:
    result = _run("list")
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row["name"] for row in rows] == NAMES
    assert all({"name", "spec", "config", "paths"} <= set(row) for row in rows)
    assert all("specs/**" in row["paths"] for row in rows)


def test_rows_prints_name_spec_and_config_in_run_order() -> None:
    result = _run("rows")
    expected = [f"{shard['name']}\t{shard['spec']}\t{shard['config']}" for shard in DATA["shards"]]
    assert result.stdout.splitlines() == expected


def test_rows_of_one_shard_prints_that_shard() -> None:
    result = _run("rows", "--shard", "recent")
    assert result.stdout.split() == [
        "recent",
        "specs/daily_prepare.tla",
        "specs/daily_prepare_recent.cfg",
    ]


def test_an_unknown_shard_exits_two_and_names_the_shards() -> None:
    result = _run("rows", "--shard", "bogus")
    assert result.returncode == 2
    assert "'bogus' is not a shard" in result.stderr
    assert "stall" in result.stderr


def test_plan_reads_the_changed_paths_from_stdin() -> None:
    assert json.loads(_run("plan", stdin="README.md\n").stdout) == []
    assert json.loads(_run("plan", stdin="specs/daily_prepare.tla\n").stdout) == NAMES


def test_plan_all_selects_every_shard_without_input() -> None:
    assert json.loads(_run("plan", "--all").stdout) == NAMES


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "cannot read the shard map"),
        ("[]", "must be an object"),
        (
            '{"always": [], "covers": ["a"], "shards": []}',
            "always must be a list with one string or more",
        ),
        ('{"always": ["a"], "covers": ["a"], "shards": []}', "one shard or more"),
        (
            '{"always": ["a"], "covers": ["a"], "shards": [{"name": "x", "spec": "s"}]}',
            "needs a non-empty config",
        ),
        (
            '{"always": ["a"], "covers": ["a"], "shards": ['
            '{"name": "x", "spec": "s", "config": "c"},'
            ' {"name": "x", "spec": "s", "config": "d"}]}',
            "listed twice",
        ),
    ],
)
def test_a_bad_map_raises_an_error_that_names_the_fault(
    tmp_path: Path, text: str, message: str
) -> None:
    path = tmp_path / "shards.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        shards.load_map(path)
