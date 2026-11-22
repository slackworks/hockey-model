"""Tests for scripts/tlc_shards.py and the shard map specs/shards.json."""

import fnmatch
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


def test_every_covered_pattern_matches_a_file() -> None:
    files = [path.relative_to(ROOT).as_posix() for path in ROOT.glob("src/**/*") if path.is_file()]
    files += [path.relative_to(ROOT).as_posix() for path in (ROOT / "scripts").glob("*")]
    for pattern in DATA["covers"]:
        assert any(shards.matches(path, [pattern]) for path in files), (
            f"{pattern} is in covers of specs/shards.json and matches no file"
        )


def test_a_change_of_the_fit_command_selects_no_shard() -> None:
    assert shards.plan(DATA, ["src/hockey_model/store_cli.py"]) == []
    assert shards.plan(DATA, ["src/hockey_model/store_cli_parsers.py"]) == []


DAILY = [s["name"] for s in DATA["shards"] if s["spec"] == "specs/daily_prepare.tla"]
UPGRADE = [s["name"] for s in DATA["shards"] if s["spec"] == "specs/store_upgrade.tla"]
REGISTRY = [s["name"] for s in DATA["shards"] if s["spec"] == "specs/experiment_registry.tla"]


def test_a_change_of_the_refresh_command_selects_the_daily_prepare_shards() -> None:
    assert shards.plan(DATA, ["src/hockey_model/store_cli_refresh.py"]) == DAILY


def test_a_change_of_the_upgrade_code_selects_the_upgrade_shard_only() -> None:
    for path in ("src/hockey_model/store/upgrade.py", "src/hockey_model/store/db.py"):
        assert shards.plan(DATA, [path]) == UPGRADE


def test_a_change_of_the_registry_code_selects_the_registry_shard_only() -> None:
    for path in (
        "src/hockey_model/experiments/history.py",
        "src/hockey_model/experiments/looks.py",
    ):
        assert shards.plan(DATA, [path]) == REGISTRY


def test_a_change_of_a_registry_record_selects_the_registry_shard() -> None:
    assert shards.plan(DATA, ["experiments/0001-first-look.json"]) == REGISTRY


def test_a_change_of_a_spec_selects_the_shards_that_run_it_and_a_config_selects_its_shard() -> None:
    assert shards.plan(DATA, ["specs/daily_prepare.tla"]) == DAILY
    assert shards.plan(DATA, ["specs/store_upgrade.tla"]) == UPGRADE
    assert shards.plan(DATA, ["specs/experiment_registry.tla"]) == REGISTRY
    assert shards.plan(DATA, ["specs/daily_prepare_recent.cfg"]) == ["recent"]


def test_a_shard_with_its_own_covers_ignores_the_shared_covers() -> None:
    data = {
        "always": ["a/**"],
        "covers": ["shared.py"],
        "shards": [
            {"name": "one", "spec": "s1", "config": "c1"},
            {"name": "two", "spec": "s2", "config": "c2", "covers": ["own.py"]},
        ],
    }
    assert shards.plan(data, ["shared.py"]) == ["one"]
    assert shards.plan(data, ["own.py"]) == ["two"]
    assert shards.plan(data, ["a/x"]) == ["one", "two"]


@pytest.mark.parametrize(
    "path", ["scripts/tlc.sh", ".github/workflows/tla.yml", "specs/shards.json"]
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
    assert all(row["spec"] in row["paths"] and row["config"] in row["paths"] for row in rows)


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
    assert json.loads(_run("plan", stdin="specs/daily_prepare.tla\n").stdout) == DAILY


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


@given(
    pattern=st.sampled_from(["*.py", "a/*.py", "a/b?.py", "[ab]/c.py", "a/**", "x"]),
    path=st.text(alphabet="abc/.py", max_size=12),
)
def test_a_pattern_matches_like_a_shell_glob_or_a_directory_prefix(pattern: str, path: str) -> None:
    if pattern.endswith("/**"):
        expected = path.startswith(pattern[:-2])
    else:
        expected = fnmatch.fnmatchcase(path, pattern)
    assert shards.matches(path, [pattern]) == expected


_PATHS = st.sampled_from(
    [
        "README.md",
        "specs/daily_prepare.tla",
        "scripts/tlc.sh",
        "scripts/daily-prepare.sh",
        "src/hockey_model/store/refresh_nhl.py",
        "src/hockey_model/store_cli.py",
    ]
)


@given(first=st.lists(_PATHS), second=st.lists(_PATHS))
def test_the_plan_of_two_changes_is_the_union_of_their_plans(
    first: list[str], second: list[str]
) -> None:
    both = set(shards.plan(DATA, first + second))
    assert both == set(shards.plan(DATA, first)) | set(shards.plan(DATA, second))
    assert shards.plan(DATA, first + second) == [name for name in NAMES if name in both]


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(
            st.sampled_from(["always", "covers", "shards", "name", "spec", "config"]), children
        )
    ),
    max_leaves=12,
)


@given(value=_JSON)
def test_load_map_returns_a_checked_map_or_raises_a_value_error(
    tmp_path_factory: pytest.TempPathFactory, value: object
) -> None:
    path = tmp_path_factory.mktemp("map") / "shards.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    try:
        data = shards.load_map(path)
    except ValueError:
        return
    assert data["shards"] and data["always"] and data["covers"]
