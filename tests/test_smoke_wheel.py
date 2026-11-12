"""Tests for the prediction check and the wheel rewrite in scripts/smoke_wheel.py."""

import importlib.util
import zipfile
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
        "missing": [{"kind": "goalie_unknown"}, {"kind": "xg_form"}],
    }
    return {**base, **changes}


def test_a_sound_prediction_passes() -> None:
    smoke._check_prediction(_prediction())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"home_win": 0.6}, "do not sum to 1"),
        ({"home_win": float("nan"), "away_win": float("nan")}, "not finite"),
        ({"home_win": "0.55"}, "not a number"),
        ({"home_win": True}, "not a number"),
        ({"home_win": None}, "not a number"),
        ({"home_goals": {"low": 3.0, "point": 2.0, "high": 4.0}}, "not ordered"),
        ({"away_goals": {"low": 1.0, "point": 2.0, "high": float("inf")}}, "not finite"),
        ({"home_goals": [1.0]}, "has no field"),
        ({"home_goals": {"low": 1.0, "point": 2.0}}, "has no field"),
        ({"away_win": float("inf")}, "not finite"),
        ({"missing": {}}, "not a list"),
        ({"missing": [{"note": "x"}]}, "has no field"),
        ({"missing": [{"kind": "rest_unknown"}]}, "must report"),
        ({"missing": []}, "must report"),
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
    before = wheel.read_bytes()
    with pytest.raises(smoke.SmokeError, match="no file with that name") as caught:
        smoke._drop_from_wheel(wheel, "schema.sql")
    assert caught.value.setup
    assert wheel.read_bytes() == before


_NAMES = st.sampled_from(["a.sql", "b.sql", "c.txt", "d.py"])
_DIRS = st.sampled_from(["", "p/", "p/q/", "r/"])


@given(st.lists(st.tuples(_DIRS, _NAMES), unique=True, max_size=8), _NAMES)
def test_drop_keeps_exactly_the_members_with_another_file_name(
    tmp_path_factory: pytest.TempPathFactory, members: list[tuple[str, str]], name: str
) -> None:
    paths = [f"{folder}{file}" for folder, file in members]
    wheel = _wheel(tmp_path_factory.mktemp("w") / "w.whl", paths)
    before = wheel.read_bytes()
    expected = [p for p in paths if Path(p).name != name]
    if len(expected) == len(paths):
        with pytest.raises(smoke.SmokeError, match="no file with that name"):
            smoke._drop_from_wheel(wheel, name)
        assert wheel.read_bytes() == before
        return
    smoke._drop_from_wheel(wheel, name)
    with zipfile.ZipFile(wheel) as archive:
        assert archive.namelist() == expected
        assert all(archive.read(p) == b"data" for p in expected)


_FINITE = st.floats(min_value=0.0, max_value=20.0)
_KINDS = st.lists(st.sampled_from(sorted(smoke.EXPECTED_MISSING)), min_size=2).filter(
    lambda kinds: set(kinds) == smoke.EXPECTED_MISSING
)


@st.composite
def _goals(draw: st.DrawFn) -> dict[str, float]:
    low, point, high = sorted(draw(st.lists(_FINITE, min_size=3, max_size=3)))
    return {"low": low, "point": point, "high": high}


@given(st.floats(min_value=0.0, max_value=1.0), _goals(), _goals(), _KINDS)
def test_a_well_formed_prediction_always_passes(
    home: float, home_goals: dict[str, float], away_goals: dict[str, float], kinds: list[str]
) -> None:
    smoke._check_prediction(
        {
            "home_win": home,
            "away_win": 1.0 - home,
            "home_goals": home_goals,
            "away_goals": away_goals,
            "missing": [{"kind": kind} for kind in kinds],
        }
    )


_JSON = st.recursive(
    st.none() | st.booleans() | st.floats(allow_nan=True) | st.text(max_size=5),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner),
    max_leaves=10,
)


@given(_JSON)
def test_any_json_value_passes_or_raises_smoke_error_only(value: object) -> None:
    try:
        smoke._check_prediction(value)
    except smoke.SmokeError:
        return
    pytest.fail(f"a random JSON value passed the check: {value!r}")
