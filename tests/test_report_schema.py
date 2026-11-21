"""Tests for the published report contract: the schema, the version, and the build index."""

import json
import re
import sqlite3
import tempfile
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path
from report_script_support import SCRIPTS, valid_report
from seeding import add_game, add_team_form, artifact, utc

from hockey_model import cli
from hockey_model.daily_report import (
    DailyLine,
    DailyReport,
    ModelIdentity,
    build_report,
    model_identity,
    report_json,
    write_report,
)
from hockey_model.game import (
    DegradedInput,
    GamePrediction,
    MissingInput,
    MissingKind,
    TrainingInfo,
    TrainingMembership,
)
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.logistic import ELO_ONLY
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.report_schema import (
    REPORT_SCHEMA_FILE,
    SCHEMA_VERSION,
    _schema,
    add_build,
    check_index,
    check_report,
    index_entry,
    version_file,
    write_index,
)
from hockey_model.sources.nhl import GameRow
from hockey_model.store.timestamps import iso_utc
from hockey_model.types import HockeyModelError, Projection

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


def test_a_build_is_named_by_its_generated_time(built: dict[str, Any]) -> None:
    assert version_file(built) == FILE_1
    other = _later(built, "2026-01-20T18:05:09.123456+00:00", "c" * 64)
    assert version_file(other) == "report-2026-01-20-180509Z.json"


def test_the_first_build_makes_an_index_of_one(built: dict[str, Any]) -> None:
    index = add_build(None, built)
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
    first = add_build(None, built)
    second = add_build(first, _later(built, "2026-01-20T15:00:00.000000+00:00", "c" * 64))
    third = add_build(second, _later(built, "2026-01-20T18:00:00.000000+00:00", "d" * 64))
    assert [build["file"] for build in third["builds"]] == [FILE_1, FILE_2, FILE_3]
    assert third["builds"][0] == first["builds"][0]


def test_the_builds_stay_in_the_order_of_their_time(built: dict[str, Any]) -> None:
    late = _later(built, "2026-01-20T18:00:00.000000+00:00", "d" * 64)
    index = add_build(add_build(None, late), built)
    assert [build["file"] for build in index["builds"]] == [FILE_1, FILE_3]


def test_the_same_build_twice_makes_one_entry(built: dict[str, Any]) -> None:
    once = add_build(None, built)
    assert add_build(once, built) == once


def test_a_report_with_no_model_has_null_identity_in_the_index(built: dict[str, Any]) -> None:
    built["model"] = None
    entry = index_entry(built)
    assert entry["code_sha"] is None and entry["fit_sha256"] is None
    check_index(add_build(None, built))


def test_an_invalid_report_makes_no_index(built: dict[str, Any]) -> None:
    del built["inputs_sha256"]
    with pytest.raises(HockeyModelError, match=REPORT_SCHEMA_FILE):
        add_build(None, built)


def test_a_build_of_another_day_is_refused(built: dict[str, Any]) -> None:
    first = add_build(None, built)
    other = deepcopy(built)
    other["date"] = "2026-01-21"
    with pytest.raises(HockeyModelError, match="cannot take a build of 2026-01-21"):
        add_build(first, other)


@pytest.mark.parametrize("field", BUILD_FIELDS)
def test_an_index_entry_missing_a_field_fails(built: dict[str, Any], field: str) -> None:
    index = add_build(None, built)
    del index["builds"][0][field]
    with pytest.raises(HockeyModelError, match=rf"'{field}' is a required property"):
        check_index(index)


def test_an_index_with_no_build_fails(built: dict[str, Any]) -> None:
    index = add_build(None, built)
    index["builds"] = []
    with pytest.raises(HockeyModelError, match=r"\$\.builds in .*\(rule 'minItems'\)"):
        check_index(index)


def test_a_missing_schema_file_names_the_file_and_the_fix() -> None:
    with pytest.raises(HockeyModelError, match=r"nope\.schema\.json.*reinstall the hockey-model"):
        _schema("nope.schema.json")


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_write_index_writes_the_file_and_names_the_build(
    built: dict[str, Any], tmp_path: Path
) -> None:
    first = tmp_path / "index-1.json"
    second = tmp_path / "index-2.json"
    assert write_index(_write(tmp_path / "r1.json", built), "2026-01-20", None, first) == (
        1,
        FILE_1,
    )
    later = _later(built, "2026-01-20T15:00:00.000000+00:00", "c" * 64)
    assert write_index(_write(tmp_path / "r2.json", later), "2026-01-20", first, second) == (
        2,
        FILE_2,
    )
    check_index(json.loads(second.read_text(encoding="utf-8")))


def test_write_index_refuses_a_report_of_another_day(built: dict[str, Any], tmp_path: Path) -> None:
    out = tmp_path / "index.json"
    with pytest.raises(HockeyModelError, match="is for 2026-01-20, not 2026-01-21"):
        write_index(_write(tmp_path / "r.json", built), "2026-01-21", None, out)
    assert not out.exists()


def test_write_index_names_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(HockeyModelError, match="report-index: cannot read the report"):
        write_index(tmp_path / "none.json", "2026-01-20", None, tmp_path / "out.json")


def _index_command(report: Path, out: Path, day: str = "2026-01-20") -> list[str]:
    return ["report-index", "--report", str(report), "--date", day, "--out", str(out)]


def test_the_report_index_command_prints_the_build_file(
    built: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "index.json"
    assert cli.main(_index_command(_write(tmp_path / "r.json", built), out)) == 0
    assert json.loads(capsys.readouterr().out) == {"out": str(out), "builds": 1, "file": FILE_1}


def test_the_report_index_command_refuses_an_invalid_report(
    built: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del built["games"]
    out = tmp_path / "index.json"
    assert cli.main(_index_command(_write(tmp_path / "r.json", built), out)) != 0
    assert "'games' is a required property" in capsys.readouterr().err
    assert not out.exists()


_MOMENTS = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2099, 12, 31), timezones=st.just(UTC)
)
_HEX = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
_MODELS = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {
            "features": st.lists(st.text(max_size=6), max_size=3),
            "fit_sha256": _HEX,
            "code_sha": st.one_of(st.none(), st.text(max_size=12)),
        }
    ),
)


@st.composite
def _reports(draw: st.DrawFn) -> dict[str, Any]:
    """Draw a schema-valid report of the default day, with a drawn time, hash, and model."""
    report = valid_report(iso_utc(draw(_MOMENTS)), inputs_sha256=draw(_HEX), model=draw(_MODELS))
    return json.loads(json.dumps(report))


_BATCHES = st.lists(_reports(), min_size=1, max_size=5, unique_by=version_file)


@given(_reports())
def test_an_index_entry_copies_the_identity_of_its_report(report: dict[str, Any]) -> None:
    entry = index_entry(report)
    model = report["model"]
    assert entry["file"] == version_file(report)
    assert entry["generated_at"] == report["generated_at"]
    assert entry["inputs_sha256"] == report["inputs_sha256"]
    assert entry["schema_version"] == report["schema_version"]
    assert entry["fit_sha256"] == (model["fit_sha256"] if model else None)
    assert entry["code_sha"] == (model["code_sha"] if model else None)


def _fold(batch: list[dict[str, Any]]) -> dict[str, Any]:
    index = None
    for report in batch:
        index = add_build(index, report)
    assert index is not None
    return index


@given(_BATCHES)
def test_an_index_keeps_every_build_in_time_order(batch: list[dict[str, Any]]) -> None:
    index = _fold(batch)
    assert sorted(b["file"] for b in index["builds"]) == sorted(version_file(r) for r in batch)
    assert [b["generated_at"] for b in index["builds"]] == sorted(r["generated_at"] for r in batch)


@given(_BATCHES, st.data())
def test_adding_a_listed_build_again_changes_nothing(
    batch: list[dict[str, Any]], data: st.DataObject
) -> None:
    index = _fold(batch)
    again = data.draw(st.sampled_from(batch))
    assert add_build(index, again) == index


@given(_BATCHES)
def test_write_index_writes_what_add_build_returns(batch: list[dict[str, Any]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        index = None
        for number, report in enumerate(batch):
            report_path = _write(root / f"report-{number}.json", report)
            existing = root / f"index-{number - 1}.json" if number else None
            out = root / f"index-{number}.json"
            count, file_name = write_index(report_path, "2026-11-04", existing, out)
            index = add_build(index, report)
            assert json.loads(out.read_text(encoding="utf-8")) == index
            assert (count, file_name) == (len(index["builds"]), version_file(report))


_TEAMS = ["TOR", "BOS", "NYR", "MTL", "CHI", "DET"]
_POINT = Projection(3.0, 2.0, 4.0, 0.8)
_TEXT = st.text(max_size=12)
_KINDS = st.sampled_from(list(MissingKind))
_MISSING = st.builds(MissingInput, _KINDS, st.one_of(st.none(), _TEXT), _TEXT)
_DEGRADED = st.builds(DegradedInput, _TEXT, _KINDS, st.lists(_TEXT, max_size=2).map(tuple))
_TRAINING = st.one_of(
    st.none(),
    st.builds(
        TrainingInfo,
        st.integers(min_value=1, max_value=1000),
        st.one_of(st.none(), st.integers(min_value=19001901, max_value=20992100)),
        st.sampled_from(list(TrainingMembership)),
    ),
)


@st.composite
def _lines(draw: st.DrawFn) -> DailyLine:
    home, away = draw(st.lists(st.sampled_from(_TEAMS), min_size=2, max_size=2, unique=True))
    row = GameRow(
        draw(st.integers(min_value=1, max_value=10**10)),
        20252026,
        2,
        draw(_MOMENTS),
        home,
        away,
        draw(st.sampled_from(["FUT", "LIVE", "OFF", "PPD"])),
        None,
        None,
        None,
    )
    if draw(st.booleans()):
        return DailyLine(row, None, draw(_TEXT))
    chance = draw(st.floats(min_value=0.0, max_value=1.0))
    prediction = GamePrediction(
        chance,
        1.0 - chance,
        _POINT,
        _POINT,
        tuple(draw(st.lists(_MISSING, max_size=3))),
        tuple(draw(st.lists(_TEXT, max_size=2))),
        draw(st.booleans()),
        tuple(draw(st.lists(_DEGRADED, max_size=2))),
        draw(_TRAINING),
    )
    return DailyLine(row, prediction, "")


_IDENTITIES = st.one_of(
    st.none(),
    st.builds(
        ModelIdentity,
        st.lists(_TEXT, max_size=3).map(tuple),
        _HEX,
        st.one_of(st.none(), _TEXT),
    ),
)
_DAILY_REPORTS = st.builds(
    DailyReport,
    st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
    _MOMENTS,
    st.floats(min_value=0.01, max_value=0.99),
    st.lists(_lines(), max_size=4).map(tuple),
    _IDENTITIES,
    st.lists(_TEXT, max_size=2).map(tuple),
)


@given(_DAILY_REPORTS)
def test_every_report_the_code_can_build_passes_the_schema(report: DailyReport) -> None:
    data = report_json(report)
    check_report(data)
    assert json.loads(json.dumps(data)) == data
    assert len({tuple(sorted(game)) for game in data["games"]}) <= 1


def test_the_shell_name_pattern_matches_the_names_the_code_writes(built: dict[str, Any]) -> None:
    """daily-report.sh finds the newest build by name, so its pattern must follow the code."""
    script = require_path(SCRIPTS / "daily-report.sh").read_text(encoding="utf-8")
    found = re.search(r'grep -E "(\^report-\$\{day\}-\[0-9\]\{6\}Z\\\.json\$)"', script)
    assert found, "daily-report.sh no longer holds the build name pattern; update this test"
    pattern = found.group(1).replace("${day}", built["date"])
    assert re.fullmatch(pattern, version_file(built))
