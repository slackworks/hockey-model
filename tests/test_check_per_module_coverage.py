"""Tests for the per-module coverage gate in scripts/check_per_module_coverage.py."""

import importlib.util
import json
import math
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "check_per_module_coverage.py")


def _load_gate() -> ModuleType:
    """Import the script by path, because scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("check_per_module_coverage", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _report(files: dict[str, object]) -> dict[str, object]:
    return {"files": files}


def _entry(percent: object) -> dict[str, object]:
    return {"summary": {"percent_covered": percent}}


def test_source_percentages_keeps_only_src_files() -> None:
    data = _report({"src/pkg/a.py": _entry(91.5), "tests/test_a.py": _entry(10.0)})
    assert gate.source_percentages(data) == {"src/pkg/a.py": 91.5}


@given(
    percents=st.dictionaries(
        st.from_regex(r"[a-z]{1,8}\.py", fullmatch=True),
        st.floats(min_value=0, max_value=100, allow_nan=False),
        max_size=6,
    ),
)
def test_source_percentages_round_trips_every_src_percent(percents: dict[str, float]) -> None:
    files: dict[str, object] = {f"src/pkg/{name}": _entry(pct) for name, pct in percents.items()}
    files["tests/test_x.py"] = _entry(0.0)
    result = gate.source_percentages(_report(files))
    assert result == {f"src/pkg/{name}": pct for name, pct in percents.items()}


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"files": []},
        {"files": {"src/a.py": None}},
        {"files": {"src/a.py": {}}},
        {"files": {"src/a.py": {"summary": None}}},
        {"files": {"src/a.py": _entry(None)}},
        {"files": {"src/a.py": _entry(True)}},
        {"files": {"src/a.py": _entry("80")}},
        {"files": {"src/a.py": _entry(math.inf)}},
    ],
)
def test_source_percentages_rejects_malformed_reports(data: object) -> None:
    with pytest.raises(ValueError):
        gate.source_percentages(data)


def test_load_report_rejects_nan_literal(tmp_path: Path) -> None:
    report = tmp_path / "coverage.json"
    report.write_text('{"files": {"src/a.py": {"summary": {"percent_covered": NaN}}}}')
    with pytest.raises(ValueError, match="non-finite literal NaN"):
        gate.load_report(report)


def test_load_report_names_the_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run pytest first"):
        gate.load_report(tmp_path / "absent.json")


def test_load_report_round_trips_valid_json(tmp_path: Path) -> None:
    report = tmp_path / "coverage.json"
    payload = _report({"src/a.py": _entry(50.0)})
    report.write_text(json.dumps(payload))
    assert gate.load_report(report) == payload


def _write_pyproject(tmp_path: Path, addopts: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[tool.pytest.ini_options]\naddopts = "{addopts}"\n')
    return path


def test_read_floor_reads_the_aggregate_flag(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, "--cov=hockey_model --cov-fail-under=75")
    assert gate.read_floor(path) == 75.0


@pytest.mark.parametrize("addopts", ["--cov=hockey_model", "--cov-fail-under=101"])
def test_read_floor_rejects_missing_or_out_of_range(tmp_path: Path, addopts: str) -> None:
    with pytest.raises(ValueError):
        gate.read_floor(_write_pyproject(tmp_path, addopts))
