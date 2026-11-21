"""Tests for the report-compare step: does a built report have the inputs of the published one."""

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.cli import main
from hockey_model.report_gate import REASON_REPLACE, REASON_UNCHANGED, compare_inputs
from hockey_model.types import HockeyModelError

HASH_A = "a" * 64
HASH_B = "b" * 64


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _compare(tmp_path: Path, built: object, published: object) -> dict[str, object]:
    return compare_inputs(
        _write(tmp_path, "built.json", built), _write(tmp_path, "pub.json", published)
    )


def test_equal_hashes_are_unchanged(tmp_path: Path) -> None:
    answer = _compare(tmp_path, {"inputs_sha256": HASH_A}, {"inputs_sha256": HASH_A})
    assert answer == {"unchanged": True, "baseline": True, "reason": REASON_UNCHANGED}


def test_different_hashes_replace(tmp_path: Path) -> None:
    answer = _compare(tmp_path, {"inputs_sha256": HASH_A}, {"inputs_sha256": HASH_B})
    assert answer == {"unchanged": False, "baseline": True, "reason": REASON_REPLACE}


@pytest.mark.parametrize(
    "published",
    [
        pytest.param({}, id="no_field"),
        pytest.param({"inputs_sha256": None}, id="null"),
        pytest.param({"inputs_sha256": ""}, id="empty"),
        pytest.param({"inputs_sha256": 7}, id="not_a_string"),
    ],
)
def test_a_published_report_without_a_hash_counts_as_changed(
    tmp_path: Path, published: object
) -> None:
    answer = _compare(tmp_path, {"inputs_sha256": HASH_A}, published)
    assert answer == {"unchanged": False, "baseline": False, "reason": REASON_REPLACE}


@pytest.mark.parametrize(
    "built",
    [
        pytest.param({}, id="no_field"),
        pytest.param({"inputs_sha256": ""}, id="empty"),
        pytest.param({"inputs_sha256": 7}, id="not_a_string"),
    ],
)
def test_a_built_report_without_a_hash_is_an_error(tmp_path: Path, built: object) -> None:
    with pytest.raises(HockeyModelError, match="built report .* has no inputs_sha256"):
        _compare(tmp_path, built, {"inputs_sha256": HASH_A})


def test_a_report_that_is_not_an_object_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(HockeyModelError, match="published report .* is not a JSON object"):
        _compare(tmp_path, {"inputs_sha256": HASH_A}, [HASH_A])


def test_a_missing_or_malformed_file_is_an_error(tmp_path: Path) -> None:
    built = _write(tmp_path, "built.json", {"inputs_sha256": HASH_A})
    with pytest.raises(HockeyModelError, match="cannot read the published report"):
        compare_inputs(built, tmp_path / "absent.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(HockeyModelError, match="cannot read the published report"):
        compare_inputs(built, broken)


def test_the_command_prints_the_answer(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    built = _write(tmp_path, "built.json", {"inputs_sha256": HASH_A})
    published = _write(tmp_path, "pub.json", {"inputs_sha256": HASH_A})
    code = main(["report-compare", "--built", str(built), "--published", str(published)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["reason"] == REASON_UNCHANGED


def test_the_command_fails_with_the_error_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built = _write(tmp_path, "built.json", {})
    published = _write(tmp_path, "pub.json", {})
    code = main(["report-compare", "--built", str(built), "--published", str(published)])
    assert code != 0
    assert "no inputs_sha256" in capsys.readouterr().err


_HASHES = st.text(alphabet="0123456789abcdef", min_size=1, max_size=64)


@given(built=_HASHES, published=st.one_of(st.none(), _HASHES))
def test_unchanged_exactly_when_the_published_hash_equals_the_built_hash(
    built: str, published: str | None
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        pub_payload = {} if published is None else {"inputs_sha256": published}
        answer = compare_inputs(
            _write(directory, "built.json", {"inputs_sha256": built}),
            _write(directory, "pub.json", pub_payload),
        )
    assert answer["unchanged"] is (published == built)
    assert answer["baseline"] is (published is not None)
    assert answer["reason"] == (REASON_UNCHANGED if published == built else REASON_REPLACE)
