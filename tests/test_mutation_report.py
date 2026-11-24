"""Tests for the mutation report decision in scripts/mutation_report.py."""

import importlib.util
import json
import tempfile
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "mutation_report.py")
BOT = {"is_bot": True, "login": "github-actions"}
PERSON = {"is_bot": False, "login": "someone"}


def _load_report() -> ModuleType:
    """Import the script by path, because scripts/ is not a package."""
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
        report.STATUS_FAILED, None, 524, "https://example.test/run/2", ["stats file is missing"]
    )
    assert report.MARKER in body
    assert "stats file is missing" in body


@given(
    counts=st.fixed_dictionaries(
        {
            "killed": st.integers(min_value=0, max_value=5000),
            "survived": st.integers(min_value=0, max_value=5000),
            "timeout": st.integers(min_value=0, max_value=500),
            "no_tests": st.integers(min_value=0, max_value=500),
        }
    ),
    baseline=st.integers(min_value=0, max_value=5000),
)
def test_build_body_renders_every_count_and_the_difference(
    counts: dict[str, int], baseline: int
) -> None:
    # The difference cell is the one derived value. A negative, zero, or large result all pass.
    total = sum(counts.values())
    stats = report.Stats(counts={**counts, "total": total}, interrupted=False)
    body = report.build_body(report.STATUS_CLEAN, stats, baseline, "https://example.test/run/5")
    difference = counts["survived"] - baseline
    row = (
        f"| {counts['killed']} | {counts['survived']} | {counts['timeout']} | "
        f"{total} | {baseline} | {difference:+d} |"
    )
    assert row in body


def test_build_body_lists_each_cause_under_the_counts() -> None:
    # A partial run shows its counts and its causes, so it never reads as a clean table.
    stats = report.Stats(counts=_counts(), interrupted=False)
    body = report.build_body(
        report.STATUS_FAILED, stats, 524, "https://example.test/run/6", ["a shard did not finish"]
    )
    assert "| 1898 | 500 | 2 | 2400 | 524 | -24 |" in body
    assert "a shard did not finish" in body


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


def test_find_open_report_raises_at_the_cap() -> None:
    # At the cap an open report can be missing from the list, so a second one would open.
    issues = [{"number": n, "body": None, "author": BOT} for n in range(1, 11)]
    with pytest.raises(ValueError, match="reaches the 10 limit"):
        report.find_open_report(json.dumps(issues), cap=10)


def test_find_open_report_accepts_a_list_one_below_the_cap() -> None:
    issues = [{"number": n, "body": None, "author": BOT} for n in range(1, 10)]
    assert report.find_open_report(json.dumps(issues), cap=10) is None


@given(size=st.integers(min_value=0, max_value=50), cap=st.integers(min_value=1, max_value=50))
def test_find_open_report_accepts_a_list_smaller_than_the_cap(size: int, cap: int) -> None:
    # Every list under the cap is read. The only raise is at or above the cap.
    issues = [{"number": n, "body": None, "author": BOT} for n in range(1, size + 1)]
    if size >= cap:
        with pytest.raises(ValueError, match="limit"):
            report.find_open_report(json.dumps(issues), cap=cap)
    else:
        assert report.find_open_report(json.dumps(issues), cap=cap) is None


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


def test_status_names_the_reason_from_the_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The download outcome passes this reason, so a failed download reads apart from no file.
    out = tmp_path / "body.md"
    code = report.main(
        [
            "status",
            "--stats",
            str(tmp_path / "missing.json"),
            "--execution",
            "failure",
            "--out",
            str(out),
            "--run-url",
            "https://example.test/run/4",
            "--reason",
            "the artifact download failed",
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == report.STATUS_FAILED
    body = out.read_text(encoding="utf-8")
    assert "the artifact download failed" in body
    assert "the run did not write the stats file" in body


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        (
            {
                "gate": "success",
                "shard": "success",
                "merge": "success",
                "export": "success",
                "download": "success",
            },
            "success",
        ),
        (
            {
                "gate": "success",
                "shard": "failure",
                "merge": "success",
                "export": "success",
                "download": "success",
            },
            "failure",
        ),
        (
            {
                "gate": "failure",
                "shard": "success",
                "merge": "success",
                "export": "success",
                "download": "success",
            },
            "failure",
        ),
        (
            {
                "gate": "success",
                "shard": "success",
                "merge": "cancelled",
                "export": "success",
                "download": "success",
            },
            "failure",
        ),
        (
            {
                "gate": "success",
                "shard": "success",
                "merge": "success",
                "export": "skipped",
                "download": "success",
            },
            "failure",
        ),
        (
            {
                "gate": "success",
                "shard": "success",
                "merge": "success",
                "export": "success",
                "download": "failure",
            },
            "failure",
        ),
    ],
)
def test_execution_requires_every_job_result(results: dict[str, str], expected: str) -> None:
    stats = report.Stats(counts=_counts(), interrupted=False)
    assert report.execution_of(results, stats) == expected


def test_execution_fails_without_stats_even_when_every_job_finished() -> None:
    results = dict.fromkeys(report.RESULT_KEYS, "success")
    assert report.execution_of(results, None) == "failure"


def test_a_failed_job_names_itself_under_the_counts(tmp_path: Path) -> None:
    # The counts are valid, and the shard failure still appears, so a partial run is not clean.
    out = tmp_path / "body.md"
    code = report.main(
        [
            "status",
            "--stats",
            str(_write_stats(tmp_path / "stats.json")),
            "--out",
            str(out),
            "--run-url",
            "https://example.test/run/7",
            "--gate-result",
            "success",
            "--shard-result",
            "failure",
            "--merge-result",
            "success",
            "--export-result",
            "success",
            "--download-result",
            "success",
        ]
    )
    assert code == 0
    body = out.read_text(encoding="utf-8")
    assert report.STATUS_FAILED in body
    assert "the shard job did not finish (failure)" in body


def test_an_interrupted_run_names_itself(tmp_path: Path) -> None:
    out = tmp_path / "body.md"
    stats = _write_stats(tmp_path / "stats.json", check_was_interrupted_by_user=True)
    code = report.main(
        [
            "status",
            "--stats",
            str(stats),
            "--execution",
            "success",
            "--out",
            str(out),
            "--run-url",
            "https://example.test/run/8",
        ]
    )
    assert code == 0
    assert "the run was interrupted" in out.read_text(encoding="utf-8")


def test_a_short_count_names_the_mutants_that_did_not_finish(tmp_path: Path) -> None:
    out = tmp_path / "body.md"
    stats = _write_stats(tmp_path / "stats.json", total=2500)
    code = report.main(
        [
            "status",
            "--stats",
            str(stats),
            "--execution",
            "success",
            "--out",
            str(out),
            "--run-url",
            "https://example.test/run/9",
        ]
    )
    assert code == 0
    assert "100 mutant(s) did not finish" in out.read_text(encoding="utf-8")


def test_a_clean_run_reports_no_cause() -> None:
    stats = report.Stats(counts=_counts(), interrupted=False)
    results = dict.fromkeys(report.RESULT_KEYS, "success")
    assert report.causes(stats, report.STATUS_CLEAN, ["a caller reason"], results) == []


def test_status_needs_execution_or_every_job_result(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        report.main(["status", "--stats", str(_write_stats(tmp_path / "stats.json"))])
    assert caught.value.code == 2


def test_status_refuses_a_partial_set_of_job_results(tmp_path: Path) -> None:
    argv = [
        "status",
        "--stats",
        str(_write_stats(tmp_path / "stats.json")),
        "--shard-result",
        "success",
    ]
    with pytest.raises(SystemExit) as caught:
        report.main(argv)
    assert caught.value.code == 2


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


def _write_timeouts(path: Path, count: int) -> Path:
    items = [
        {"mutant": f"m.x_f__mutmut_{n}", "estimated_s": 3.2, "limit_s": 26} for n in range(count)
    ]
    path.write_text(
        json.dumps({"timeout_multiplier": 3.0, "timeout_constant": 1.0, "timeouts": items}),
        encoding="utf-8",
    )
    return path


def test_the_timeout_section_lists_each_mutant_with_its_estimate_and_limit(tmp_path: Path) -> None:
    section = report.timeouts_section(_write_timeouts(tmp_path / "t.json", 2))
    assert "- `m.x_f__mutmut_0`: estimated 3.2 s, limit 26 s" in section
    assert "- `m.x_f__mutmut_1`: estimated 3.2 s, limit 26 s" in section
    assert "moves from run to run" in section


def test_a_run_without_a_timeout_has_no_timeout_section(tmp_path: Path) -> None:
    assert report.timeouts_section(_write_timeouts(tmp_path / "t.json", 0)) == ""


@given(count=st.integers(min_value=0, max_value=60))
def test_the_section_shows_at_most_the_cap_and_counts_the_rest(count: int) -> None:
    with tempfile.TemporaryDirectory() as directory:
        section = report.timeouts_section(_write_timeouts(Path(directory) / "t.json", count))
    shown = section.count("\n- `")
    assert shown == min(count, report.TIMEOUT_LIST_CAP)
    more = count - report.TIMEOUT_LIST_CAP
    assert (f"and {more} more" in section) == (more > 0)


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"timeouts": 3}', '{"timeouts": [{"mutant": "m"}]}'],
)
def test_a_timeout_file_that_cannot_be_read_is_named_in_the_section(
    tmp_path: Path, text: str
) -> None:
    path = tmp_path / "t.json"
    path.write_text(text, encoding="utf-8")
    section = report.timeouts_section(path)
    assert "The list of timed-out mutants could not be read" in section


def test_a_missing_timeout_file_is_named_in_the_section(tmp_path: Path) -> None:
    section = report.timeouts_section(tmp_path / "none.json")
    assert "The list of timed-out mutants could not be read" in section


def test_the_status_command_adds_the_section_and_keeps_the_status(tmp_path: Path) -> None:
    stats = _write_stats(tmp_path / "stats.json")
    out = tmp_path / "body.md"
    plain = report.main(
        ["status", "--stats", str(stats), "--execution", "success", "--out", str(out)]
        + ["--run-url", "https://example.test/run"]
    )
    assert plain == 0 and "Timed-out mutants" not in out.read_text(encoding="utf-8")
    listed = tmp_path / "timeouts.json"
    _write_timeouts(listed, 1)
    code = report.main(
        ["status", "--stats", str(stats), "--execution", "success", "--out", str(out)]
        + ["--run-url", "https://example.test/run", "--timeouts", str(listed)]
    )
    assert code == 0
    assert "- `m.x_f__mutmut_0`" in out.read_text(encoding="utf-8")


def test_a_bad_timeout_file_does_not_change_the_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stats = _write_stats(tmp_path / "stats.json")
    base = ["status", "--stats", str(stats), "--execution", "success"]
    report.main(base)
    clean = capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    out = tmp_path / "body.md"
    report.main([*base, "--out", str(out), "--run-url", "u", "--timeouts", str(bad)])
    assert capsys.readouterr().out == clean


@pytest.mark.parametrize("name", ["a`b", "a\nb", "@someone", "a b", "", "x" * 301, "[x](y)"])
def test_a_mutant_name_that_is_not_a_dotted_name_is_refused(tmp_path: Path, name: str) -> None:
    path = tmp_path / "t.json"
    item = {"mutant": name, "estimated_s": 1.0, "limit_s": 9}
    path.write_text(json.dumps({"timeouts": [item]}), encoding="utf-8")
    assert "could not be read" in report.timeouts_section(path)


def test_the_class_mark_of_a_method_name_passes(tmp_path: Path) -> None:
    name = "hockey_model.mod.x\u01c1Klass\u01c1method__mutmut_3"
    path = tmp_path / "t.json"
    item = {"mutant": name, "estimated_s": 1.5, "limit_s": 9}
    path.write_text(json.dumps({"timeouts": [item]}), encoding="utf-8")
    assert f"- `{name}`: estimated 1.5 s, limit 9 s" in report.timeouts_section(path)


@pytest.mark.parametrize("seconds", ["NaN", "Infinity", "-1", "true", '"3"', "1" + "0" * 400])
def test_an_estimate_that_is_not_a_finite_number_is_refused(tmp_path: Path, seconds: str) -> None:
    path = tmp_path / "t.json"
    text = f'{{"timeouts": [{{"mutant": "m.x", "estimated_s": {seconds}, "limit_s": 9}}]}}'
    path.write_text(text, encoding="utf-8")
    assert "could not be read" in report.timeouts_section(path)


# A JSON integer has no size limit, and an integer past the float range must not crash the read.
_HUGE_INTEGERS = st.integers(min_value=10**309, max_value=10**600) | st.integers(
    min_value=-(10**600), max_value=-(10**309)
)


@given(seconds=_HUGE_INTEGERS)
def test_an_estimate_past_the_float_range_is_refused(seconds: int) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "t.json"
        item = {"mutant": "m.x", "estimated_s": seconds, "limit_s": 9}
        path.write_text(json.dumps({"timeouts": [item]}), encoding="utf-8")
        assert "is not a finite number" in report.timeouts_section(path)


_JSON = st.recursive(
    st.one_of(
        st.none(), st.booleans(), st.integers(), _HUGE_INTEGERS, st.floats(), st.text(max_size=8)
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=3), st.dictionaries(st.text(max_size=8), children, max_size=3)
    ),
    max_leaves=12,
)


@given(value=_JSON)
def test_any_json_gives_text_and_never_a_crash(value: object) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "t.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        assert isinstance(report.timeouts_section(path), str)
        try:
            found = report._read_timeouts(path)
        except ValueError:
            return
    assert all(isinstance(name, str) and limit >= 0 for name, _, limit in found)


@given(
    entries=st.lists(
        st.tuples(
            st.from_regex(r"[a-z][a-z0-9_.]{0,30}", fullmatch=True),
            st.floats(min_value=0.0, max_value=900.0),
        ),
        max_size=25,
        unique_by=lambda entry: entry[0],
    )
)
def test_what_the_script_writes_is_what_the_report_reads(entries: list[tuple[str, float]]) -> None:
    spec = importlib.util.spec_from_file_location(
        "mutation_timeouts", ROOT / "scripts" / "mutation_timeouts.py"
    )
    assert spec is not None and spec.loader is not None
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        codes = {name: 36 for name, _ in entries}
        estimates = {name: seconds for name, seconds in entries}
        (root / "m").mkdir()
        (root / "m" / "a.py.meta").write_text(
            json.dumps({"exit_code_by_key": codes, "estimated_durations_by_key": estimates}),
            encoding="utf-8",
        )
        pyproject = root / "p.toml"
        pyproject.write_text("[tool.mutmut]\ntimeout_multiplier = 3.0\n", encoding="utf-8")
        out = root / "out.json"
        argv = ["--mutants", str(root / "m"), "--pyproject", str(pyproject), "--out", str(out)]
        assert writer.main(argv) == 0
        read = report._read_timeouts(out)
        section = report.timeouts_section(out)
    assert [name for name, _, _ in read] == sorted(name for name, _ in entries)
    shown = sorted(name for name, _ in entries)[: report.TIMEOUT_LIST_CAP]
    assert all(f"- `{name}`:" in section for name in shown)


def test_a_run_with_timed_out_mutants_and_no_list_says_the_list_is_missing(
    tmp_path: Path,
) -> None:
    stats = _write_stats(tmp_path / "stats.json")
    out = tmp_path / "body.md"
    args = ["--stats", str(stats), "--execution", "success", "--out", str(out), "--run-url", "u"]
    report.main(["status", *args, "--timeouts", str(tmp_path / "none.json")])
    assert "The list of timed-out mutants could not be read" in out.read_text(encoding="utf-8")


def test_a_run_with_no_timed_out_mutant_and_no_list_says_nothing(tmp_path: Path) -> None:
    stats = _write_stats(tmp_path / "stats.json", timeout=0)
    out = tmp_path / "body.md"
    args = ["--stats", str(stats), "--execution", "success", "--out", str(out), "--run-url", "u"]
    report.main(["status", *args, "--timeouts", str(tmp_path / "none.json")])
    assert "timed-out mutants" not in out.read_text(encoding="utf-8")


def test_a_failed_gate_names_itself_in_the_body(tmp_path: Path) -> None:
    """A failed run lookup leaves the other jobs skipped, so the body must name the gate."""
    out = tmp_path / "body.md"
    code = report.main(
        [
            "status",
            "--stats",
            str(tmp_path / "absent.json"),
            "--out",
            str(out),
            "--run-url",
            "https://example.test/run/10",
            "--gate-result",
            "failure",
            "--shard-result",
            "skipped",
            "--merge-result",
            "skipped",
            "--export-result",
            "unknown",
            "--download-result",
            "skipped",
        ]
    )
    assert code == 0
    body = out.read_text(encoding="utf-8")
    assert "the gate job did not finish (failure)" in body
    assert body.index("the gate job") < body.index("the shard job")
