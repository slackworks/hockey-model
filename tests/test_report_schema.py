"""Tests for the published report contract: the schema, the version, and the build index."""

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from report_script_support import valid_report
from seeding import add_game, add_team_form, artifact, utc

from hockey_model import cli
from hockey_model.daily_report import (
    DailyReport,
    build_report,
    model_identity,
    report_json,
    write_report,
)
from hockey_model.game import MissingKind, TrainingMembership
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.logistic import ELO_ONLY
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.report_schema import (
    INDEX_SCHEMA_FILE,
    REPORT_SCHEMA_FILE,
    SCHEMA_VERSION,
    _schema,
    add_build,
    check_index,
    check_report,
    index_entry,
    write_index,
)
from hockey_model.types import HockeyModelError

DAY = date(2026, 1, 20)
NOW = utc("2026-01-20T12:00")
TOP_FIELDS = [
    "schema_version",
    "date",
    "generated_at",
    "inputs_sha256",
    "level",
    "games",
    "credits",
    "model",
    "training_gaps",
]
GAME_FIELDS = [
    "game_id",
    "away",
    "home",
    "start_utc",
    "state",
    "skipped",
    "home_win",
    "away_win",
    "made_after_start",
    "missing",
    "degraded_inputs",
    "training",
]
BUILD_FIELDS = ["file", "generated_at", "code_sha", "fit_sha256", "inputs_sha256", "schema_version"]


def _report(store: sqlite3.Connection) -> DailyReport:
    """Build a report from a seeded store: one predicted game and one postponed game."""
    add_team_form(store, 1, "TOR", 3.5, 2.0)
    add_team_form(store, 2, "BOS", 2.0, 3.0)
    add_game(store, 10, "2026-01-20T23:00", "TOR", "BOS", state="FUT")
    add_game(store, 14, "2026-01-20T22:00", "TOR", "BOS", state="PPD")
    fit = artifact(ELO_ONLY)
    report = build_report(store, NhlGameModel(store, fit, GoalieMode.GUESS), DAY, now=NOW)
    return replace(report, model=model_identity(fit.fit, "abc123"))


@pytest.fixture
def built(store: sqlite3.Connection) -> dict[str, Any]:
    return json.loads(json.dumps(report_json(_report(store))))


def _fails(payload: dict[str, Any], message: str) -> None:
    with pytest.raises(HockeyModelError, match=message):
        check_report(payload)


def test_a_built_report_passes_the_schema(built: dict[str, Any]) -> None:
    check_report(built)
    assert [game["skipped"] is None for game in built["games"]] == [False, True]


def test_the_report_carries_the_schema_version(built: dict[str, Any]) -> None:
    assert built["schema_version"] == SCHEMA_VERSION == 1


def test_a_report_without_a_model_passes(built: dict[str, Any]) -> None:
    built["model"] = None
    check_report(built)


@pytest.mark.parametrize("field", TOP_FIELDS)
def test_a_report_missing_a_top_level_field_fails_with_its_path(
    built: dict[str, Any], field: str
) -> None:
    del built[field]
    _fails(built, rf"\$ in {REPORT_SCHEMA_FILE}: '{field}' is a required property")


@pytest.mark.parametrize("field", GAME_FIELDS)
def test_a_game_missing_a_field_fails_with_its_path(built: dict[str, Any], field: str) -> None:
    del built["games"][0][field]
    _fails(built, rf"\$\.games\[0\] in {REPORT_SCHEMA_FILE}: '{field}' is a required property")


def test_an_unknown_top_level_field_fails(built: dict[str, Any]) -> None:
    built["extra"] = 1
    _fails(built, r"Additional properties are not allowed \('extra' was unexpected\)")


def test_an_unknown_game_field_fails(built: dict[str, Any]) -> None:
    built["games"][0]["extra"] = 1
    _fails(built, r"\$\.games\[0\] in .*'extra' was unexpected")


def test_an_unknown_missing_kind_fails(built: dict[str, Any]) -> None:
    built["games"][0]["missing"] = [{"kind": "bogus", "team": None, "note": "x"}]
    _fails(built, r"\$\.games\[0\]\.missing\[0\]\.kind in .*'bogus' is not one of")


def test_a_later_schema_version_fails(built: dict[str, Any]) -> None:
    built["schema_version"] = 2
    _fails(built, r"\$\.schema_version in .*\(rule 'const'\)")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("level",), 1.5),
        (("level",), "0.8"),
        (("inputs_sha256",), "abc"),
        (("generated_at",), "2026-01-20 12:00:00"),
        (("date",), "2026-02-30"),
        (("games", 0, "home_win"), 1.2),
        (("games", 0, "start_utc"), "2026-01-20T23:00:00Z"),
        (("games", 0, "game_id"), "10"),
        (("model", "fit_sha256"), "XYZ"),
    ],
)
def test_a_wrong_value_fails(built: dict[str, Any], path: tuple, value: object) -> None:
    target: Any = built
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(HockeyModelError, match=REPORT_SCHEMA_FILE):
        check_report(built)


def test_the_missing_kinds_in_the_schema_match_the_code() -> None:
    kinds = _schema(REPORT_SCHEMA_FILE)["$defs"]["missing_kind"]["enum"]
    assert sorted(kinds) == sorted(kind.value for kind in MissingKind)


def test_the_training_memberships_in_the_schema_match_the_code() -> None:
    training = _schema(REPORT_SCHEMA_FILE)["$defs"]["game"]["properties"]["training"]
    assert sorted(training["properties"]["membership"]["enum"]) == sorted(
        member.value for member in TrainingMembership
    )


def test_the_schema_lists_the_fields_the_report_writes(built: dict[str, Any]) -> None:
    schema = _schema(REPORT_SCHEMA_FILE)
    assert sorted(schema["required"]) == sorted(built) == sorted(TOP_FIELDS)
    assert sorted(schema["$defs"]["game"]["required"]) == sorted(built["games"][0])


def test_write_report_refuses_an_invalid_report_and_writes_no_file(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    bad = replace(_report(store), level=1.5)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(HockeyModelError, match=rf"\$\.level in {REPORT_SCHEMA_FILE}"):
        write_report(bad, out)
    assert list(out.iterdir()) == []


def test_write_report_writes_a_report_that_passes_the_schema(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    _, json_path = write_report(_report(store), tmp_path)
    check_report(json.loads(json_path.read_text(encoding="utf-8")))


FILE_1 = "report-2026-01-20-120000Z.json"
FILE_2 = "report-2026-01-20-150000Z.json"
FILE_3 = "report-2026-01-20-180000Z.json"


def _later(report: dict[str, Any], generated: str, inputs: str) -> dict[str, Any]:
    other = deepcopy(report)
    other["generated_at"] = generated
    other["inputs_sha256"] = inputs
    return other


def test_the_first_build_makes_an_index_of_one(built: dict[str, Any]) -> None:
    index = add_build(None, built, FILE_1)
    assert index["date"] == "2026-01-20"
    assert index["builds"] == [
        {
            "file": FILE_1,
            "generated_at": built["generated_at"],
            "code_sha": "abc123",
            "fit_sha256": built["model"]["fit_sha256"],
            "inputs_sha256": built["inputs_sha256"],
            "schema_version": 1,
        }
    ]


def test_a_rebuild_adds_its_entry_and_drops_none(built: dict[str, Any]) -> None:
    first = add_build(None, built, FILE_1)
    second = add_build(first, _later(built, "2026-01-20T15:00:00.000000+00:00", "c" * 64), FILE_2)
    third = add_build(second, _later(built, "2026-01-20T18:00:00.000000+00:00", "d" * 64), FILE_3)
    assert [build["file"] for build in third["builds"]] == [FILE_1, FILE_2, FILE_3]
    assert third["builds"][0] == first["builds"][0]


def test_the_builds_stay_in_the_order_of_their_time(built: dict[str, Any]) -> None:
    late = _later(built, "2026-01-20T18:00:00.000000+00:00", "d" * 64)
    index = add_build(add_build(None, late, FILE_3), built, FILE_1)
    assert [build["file"] for build in index["builds"]] == [FILE_1, FILE_3]


def test_the_same_build_twice_makes_one_entry(built: dict[str, Any]) -> None:
    once = add_build(None, built, FILE_1)
    assert add_build(once, built, FILE_1) == once


def test_a_report_with_no_model_has_null_identity_in_the_index(built: dict[str, Any]) -> None:
    built["model"] = None
    entry = index_entry(built, FILE_1)
    assert entry["code_sha"] is None and entry["fit_sha256"] is None
    check_index(add_build(None, built, FILE_1))


def test_an_invalid_report_makes_no_index(built: dict[str, Any]) -> None:
    del built["inputs_sha256"]
    with pytest.raises(HockeyModelError, match=REPORT_SCHEMA_FILE):
        add_build(None, built, FILE_1)


def test_a_build_of_another_day_is_refused(built: dict[str, Any]) -> None:
    first = add_build(None, built, FILE_1)
    other = deepcopy(built)
    other["date"] = "2026-01-21"
    with pytest.raises(HockeyModelError, match="cannot take a build of 2026-01-21"):
        add_build(first, other, FILE_2)


@pytest.mark.parametrize("name", ["report-2026-01-20.json", "report-2026-01-20-1200Z.json", "x"])
def test_a_build_file_name_must_be_a_versioned_report(built: dict[str, Any], name: str) -> None:
    with pytest.raises(HockeyModelError, match=INDEX_SCHEMA_FILE):
        add_build(None, built, name)


@pytest.mark.parametrize("field", BUILD_FIELDS)
def test_an_index_entry_missing_a_field_fails(built: dict[str, Any], field: str) -> None:
    index = add_build(None, built, FILE_1)
    del index["builds"][0][field]
    with pytest.raises(HockeyModelError, match=rf"'{field}' is a required property"):
        check_index(index)


def test_an_index_with_no_build_fails(built: dict[str, Any]) -> None:
    index = add_build(None, built, FILE_1)
    index["builds"] = []
    with pytest.raises(HockeyModelError, match=r"\$\.builds in .*\(rule 'minItems'\)"):
        check_index(index)


@given(hours=st.lists(st.integers(min_value=0, max_value=23), min_size=1, max_size=6, unique=True))
def test_the_index_lists_every_build_in_time_order(hours: list[int]) -> None:
    base = valid_report()
    index = None
    for hour in hours:
        stamp = f"{hour:02d}0000Z"
        report = _later(base, f"2026-01-20T{hour:02d}:00:00.000000+00:00", f"{hour:064x}")
        index = add_build(index, report, f"report-2026-01-20-{stamp}.json")
    assert index is not None
    assert [b["file"][-12:-6] for b in index["builds"]] == [f"{h:02d}0000" for h in sorted(hours)]


def test_write_index_writes_the_file_and_counts_the_builds(
    built: dict[str, Any], tmp_path: Path
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(built), encoding="utf-8")
    first = tmp_path / "index-1.json"
    second = tmp_path / "index-2.json"
    assert write_index(report_path, FILE_1, None, first) == 1
    assert write_index(report_path, FILE_2, first, second) == 2
    check_index(json.loads(second.read_text(encoding="utf-8")))


def test_write_index_names_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(HockeyModelError, match="report-index: cannot read the report"):
        write_index(tmp_path / "none.json", FILE_1, None, tmp_path / "out.json")


def test_the_report_index_command_refuses_an_invalid_report(
    built: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del built["games"]
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(built), encoding="utf-8")
    out = tmp_path / "index.json"
    args = ["report-index", "--report", str(report_path), "--name", FILE_1, "--out", str(out)]
    assert cli.main(args) != 0
    assert "'games' is a required property" in capsys.readouterr().err
    assert not out.exists()
