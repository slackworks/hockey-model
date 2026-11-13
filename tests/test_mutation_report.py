"""Tests for the mutation report decision in scripts/mutation_report.py."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mutation_report.py"
BOT = {"is_bot": True, "login": "github-actions"}
PERSON = {"is_bot": False, "login": "someone"}


def _load_report() -> ModuleType:
    """Import the script by path, because scripts/ is not a package."""
    if not SCRIPT.exists():
        # A mutation run copies src/ and tests/ only, so the script is absent there.
        pytest.skip("scripts/mutation_report.py is not copied", allow_module_level=True)
    spec = importlib.util.spec_from_file_location("mutation_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = _load_report()

GOOD_COUNTS = {"killed": 1898, "survived": 500, "timeout": 2, "no_tests": 0, "total": 2400}


def _counts(**changes: int) -> dict[str, int]:
    """Return counts whose total is the sum of the outcomes, unless a change names the total."""
    counts = {**GOOD_COUNTS, **changes}
    if "total" not in changes:
        counts["total"] = sum(counts[k] for k in ("killed", "survived", "timeout", "no_tests"))
    return counts


def _write_stats(path: Path, **overrides: object) -> Path:
    """Write a stats file with the good counts, overridden by keyword."""
    data: dict[str, object] = {**GOOD_COUNTS, "skipped": 0, "suspicious": 0, "segfault": 0}
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_read_stats_returns_the_counts(tmp_path: Path) -> None:
    stats = report.read_stats(_write_stats(tmp_path / "stats.json"))
    assert stats.counts == GOOD_COUNTS
    assert stats.interrupted is False


@pytest.mark.parametrize("field", ["killed", "survived", "timeout", "no_tests", "total"])
def test_read_stats_rejects_a_missing_count(tmp_path: Path, field: str) -> None:
    path = tmp_path / "stats.json"
    data = {k: v for k, v in GOOD_COUNTS.items() if k != field}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(report.MalformedStats, match=field):
        report.read_stats(path)


@pytest.mark.parametrize("bad", ["12", 1.5, -1, True, None])
def test_read_stats_rejects_a_count_that_is_not_a_whole_number(tmp_path: Path, bad: object) -> None:
    with pytest.raises(report.MalformedStats, match="survived"):
        report.read_stats(_write_stats(tmp_path / "stats.json", survived=bad))


def test_read_stats_rejects_a_file_that_is_not_json(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(report.MalformedStats, match="not JSON"):
        report.read_stats(path)


def test_read_stats_rejects_bytes_that_are_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(report.MalformedStats, match="cannot read"):
        report.read_stats(path)


def test_read_stats_rejects_a_json_array(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(report.MalformedStats, match="JSON object"):
        report.read_stats(path)


def test_read_stats_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(report.MalformedStats, match="cannot read"):
        report.read_stats(tmp_path / "absent.json")


def test_read_stats_marks_an_interrupted_run(tmp_path: Path) -> None:
    path = _write_stats(tmp_path / "stats.json", check_was_interrupted_by_user=True)
    assert report.read_stats(path).interrupted is True


def test_decide_is_clean_at_the_baseline() -> None:
    stats = report.Stats(counts=_counts(survived=524), interrupted=False)
    assert report.decide(stats, "success", baseline=524) == report.STATUS_CLEAN


def test_decide_is_a_regression_above_the_baseline() -> None:
    stats = report.Stats(counts=_counts(survived=525), interrupted=False)
    assert report.decide(stats, "success", baseline=524) == report.STATUS_REGRESSION


def test_decide_fails_when_a_shard_failed_even_with_clean_counts() -> None:
    stats = report.Stats(counts=_counts(survived=1), interrupted=False)
    assert report.decide(stats, "failure", baseline=524) == report.STATUS_FAILED


def test_decide_fails_when_there_are_no_stats() -> None:
    assert report.decide(None, "success", baseline=524) == report.STATUS_FAILED


def test_decide_fails_when_the_run_was_interrupted() -> None:
    stats = report.Stats(counts=_counts(survived=1), interrupted=True)
    assert report.decide(stats, "success", baseline=524) == report.STATUS_FAILED


@given(survived=st.integers(min_value=0, max_value=10_000), baseline=st.integers(0, 10_000))
def test_decide_matches_the_baseline_for_every_count(survived: int, baseline: int) -> None:
    stats = report.Stats(counts=_counts(survived=survived), interrupted=False)
    expected = report.STATUS_CLEAN if survived <= baseline else report.STATUS_REGRESSION
    assert report.decide(stats, "success", baseline) == expected


def test_build_body_starts_with_the_marker_and_shows_the_difference() -> None:
    stats = report.Stats(counts=_counts(survived=600), interrupted=False)
    body = report.build_body(report.STATUS_REGRESSION, stats, 524, "https://example.test/run/1")
    assert body.startswith(report.MARKER)
    assert "| 1898 | 600 | 2 | 2500 | 524 | +76 |" in body
    assert "https://example.test/run/1" in body


def test_build_body_names_the_reason_when_there_are_no_stats() -> None:
    body = report.build_body(
        report.STATUS_FAILED, None, 524, "https://example.test/run/2", "stats file is missing"
    )
    assert report.MARKER in body
    assert "stats file is missing" in body


def test_status_writes_the_body_and_prints_the_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "body.md"
    code = report.main(
        [
            "status",
            "--stats",
            str(_write_stats(tmp_path / "stats.json")),
            "--execution",
            "success",
            "--out",
            str(out),
            "--run-url",
            "https://example.test/run/3",
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == report.STATUS_CLEAN
    assert "https://example.test/run/3" in out.read_text(encoding="utf-8")


def test_status_with_out_needs_a_run_url(tmp_path: Path) -> None:
    argv = [
        "status",
        "--stats",
        str(tmp_path / "missing.json"),
        "--execution",
        "success",
        "--out",
        str(tmp_path / "body.md"),
    ]
    with pytest.raises(SystemExit) as caught:
        report.main(argv)
    assert caught.value.code == 2


def test_find_open_report_returns_the_marked_bot_issue() -> None:
    issues = [
        {"number": 7, "body": "unrelated", "author": BOT},
        {"number": 9, "body": f"{report.MARKER}\nold run", "author": BOT},
    ]
    assert report.find_open_report(json.dumps(issues)) == 9


def test_find_open_report_ignores_a_marker_copied_by_a_person() -> None:
    issues = [{"number": 9, "body": report.MARKER, "author": PERSON}]
    assert report.find_open_report(json.dumps(issues)) is None


def test_find_open_report_returns_none_when_no_issue_is_marked() -> None:
    issues = [{"number": 7, "body": None, "author": BOT}]
    assert report.find_open_report(json.dumps(issues)) is None


def test_find_open_report_returns_none_for_an_empty_list() -> None:
    assert report.find_open_report("[]") is None


@pytest.mark.parametrize(
    "payload",
    [
        '{"number": 1}',
        '[{"body": "x"}]',
        '[{"number": "7", "body": "x"}]',
        '[{"number": true, "body": "x"}]',
        '[{"number": 7, "body": 5}]',
        "not json",
    ],
)
def test_find_open_report_rejects_a_malformed_list(payload: str) -> None:
    with pytest.raises(ValueError):
        report.find_open_report(payload)


@given(number=st.integers(min_value=1, max_value=10**9))
def test_find_open_report_returns_the_number_it_was_given(number: int) -> None:
    issues = [{"number": number, "body": report.MARKER, "author": BOT}]
    assert report.find_open_report(json.dumps(issues)) == number


@pytest.mark.parametrize("delta", [1, 3, 40, -1])
def test_decide_fails_when_the_total_and_the_outcomes_disagree(delta: int) -> None:
    # A positive delta is a mutant that did not finish. A negative one means the outcome counts
    # exceed the total. Either way the counts are not trusted.
    stats = report.Stats(counts=_counts(total=2400 + delta), interrupted=False)
    assert report.decide(stats, "success", 10_000) == report.STATUS_FAILED


def test_a_stats_file_with_unfinished_mutants_fails_the_run(tmp_path: Path) -> None:
    path = _write_stats(tmp_path / "stats.json", total=2405)
    stats = report.read_stats(path)
    assert report.decide(stats, "success", 524) == report.STATUS_FAILED


def test_a_real_export_with_no_unfinished_mutants_stays_clean(tmp_path: Path) -> None:
    # Every key that mutmut 3.8 writes to its CI stats file, with no unfinished mutant.
    export = {
        "killed": 400,
        "survived": 100,
        "timeout": 5,
        "no_tests": 5,
        "skipped": 0,
        "suspicious": 0,
        "segfault": 0,
        "total": 510,
        "check_was_interrupted_by_user": False,
    }
    path = tmp_path / "stats.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    stats = report.read_stats(path)
    assert report.decide(stats, "success", 524) == report.STATUS_CLEAN


def test_a_real_export_with_a_suspicious_mutant_fails_the_run(tmp_path: Path) -> None:
    export = {
        "killed": 400,
        "survived": 100,
        "timeout": 5,
        "no_tests": 5,
        "skipped": 3,
        "suspicious": 1,
        "segfault": 0,
        "total": 514,
        "check_was_interrupted_by_user": False,
    }
    path = tmp_path / "stats.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    stats = report.read_stats(path)
    assert report.decide(stats, "success", 524) == report.STATUS_FAILED
