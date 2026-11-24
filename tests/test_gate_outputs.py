"""Tests for gate-outputs.sh, which turns the gate JSON into the step outputs."""

import itertools
import json
import os
from pathlib import Path

import pytest
from report_script_support import _run

GOOD = {"due": True, "publish": True, "date": "2026-11-04", "reason": "due"}


def _gate(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "gate.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _outputs(path: Path):
    return _run("gate-outputs.sh", {"PATH": os.environ["PATH"]}, str(path))


@pytest.mark.parametrize(("due", "publish"), list(itertools.product([True, False], repeat=2)))
def test_a_false_value_is_a_valid_output(tmp_path: Path, due: bool, publish: bool) -> None:
    """Most hourly runs have due or publish false, and they must not fail the step."""
    result = _outputs(_gate(tmp_path, GOOD | {"due": due, "publish": publish}))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"due={str(due).lower()}",
        f"publish={str(publish).lower()}",
        "day=2026-11-04",
    ]


def test_the_reason_stays_out_of_the_output(tmp_path: Path) -> None:
    result = _outputs(_gate(tmp_path, GOOD | {"reason": "a\nextra=1"}))
    assert result.returncode == 0, result.stderr
    assert "extra" not in result.stdout


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"due": "true"}, "due"),
        ({"due": None}, "due"),
        ({"publish": 1}, "publish"),
        ({"date": 20261104}, "date"),
        ({"reason": None}, "reason"),
        ({"date": "2026-11-04\nevil=1"}, "date"),
        ({"date": "tomorrow"}, "date"),
    ],
)
def test_a_wrong_type_stops_the_step_with_nothing_printed(
    tmp_path: Path, change: dict[str, object], field: str
) -> None:
    result = _outputs(_gate(tmp_path, GOOD | change))
    assert result.returncode == 1
    assert result.stdout == ""
    assert f"'{field}'" in result.stderr


@pytest.mark.parametrize("field", ["due", "publish", "date", "reason"])
def test_a_missing_field_stops_the_step(tmp_path: Path, field: str) -> None:
    payload = {key: value for key, value in GOOD.items() if key != field}
    result = _outputs(_gate(tmp_path, payload))
    assert result.returncode == 1
    assert result.stdout == ""
    assert f"'{field}'" in result.stderr


@pytest.mark.parametrize("payload", ["", "not json", "[]", "null"])
def test_a_payload_that_is_not_an_object_stops_the_step(tmp_path: Path, payload: str) -> None:
    result = _outputs(_gate(tmp_path, payload))
    assert result.returncode == 1
    assert result.stdout == ""


def test_a_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    result = _outputs(tmp_path / "none.json")
    assert result.returncode == 2
    assert "readable file" in result.stderr


def test_help_names_the_outputs() -> None:
    result = _run("gate-outputs.sh", {"PATH": os.environ["PATH"]}, "--help")
    assert result.returncode == 0
    assert "due=" in result.stdout and "publish=" in result.stdout
