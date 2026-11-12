"""Tests for the prediction check and the wheel rewrite in scripts/smoke_wheel.py."""

import importlib.util
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_wheel.py"


def _load() -> ModuleType:
    """Import the script by path, because scripts/ is not a package."""
    if not SCRIPT.exists():
        # A mutation run copies src/ and tests/ only, so the script is absent there.
        pytest.skip("scripts/smoke_wheel.py is not copied", allow_module_level=True)
    spec = importlib.util.spec_from_file_location("smoke_wheel", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load()


def _prediction(**changes: object) -> dict[str, object]:
    goals = {"low": 1.0, "point": 2.5, "high": 4.0}
    base: dict[str, object] = {
        "home_win": 0.55,
        "away_win": 0.45,
        "home_goals": goals,
        "away_goals": goals,
        "missing": [{"input": "goalie"}],
    }
    return {**base, **changes}


def test_a_sound_prediction_passes() -> None:
    smoke._check_prediction(_prediction())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"home_win": 0.6}, "do not sum to 1"),
        ({"home_win": float("nan"), "away_win": float("nan")}, "do not sum to 1"),
        ({"home_win": "0.55"}, "not numbers"),
        ({"home_goals": {"low": 3.0, "point": 2.0, "high": 4.0}}, "not ordered"),
        ({"away_goals": {"low": 1.0, "point": 2.0, "high": float("inf")}}, "not finite"),
        ({"home_goals": [1.0]}, "not an object"),
        ({"missing": []}, "must report missing inputs"),
    ],
)
def test_an_unsound_prediction_fails_with_the_reason(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(smoke.SmokeError, match=message):
        smoke._check_prediction(_prediction(**changes))


def _wheel(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "data")
    return path


def test_drop_removes_every_file_with_that_name(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "w.whl", ["p/schema.sql", "p/views.sql", "q/schema.sql"])
    smoke._drop_from_wheel(wheel, "schema.sql")
    with zipfile.ZipFile(wheel) as archive:
        assert archive.namelist() == ["p/views.sql"]


def test_drop_of_a_name_the_wheel_lacks_fails(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "w.whl", ["p/views.sql"])
    with pytest.raises(smoke.SmokeError, match="no file with that name"):
        smoke._drop_from_wheel(wheel, "schema.sql")
    assert wheel.exists()
