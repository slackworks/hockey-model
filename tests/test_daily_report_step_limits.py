"""Tests that the report step has a time limit between its budget and the job limit."""

import re
from pathlib import Path

from mutation_copy import require_path

WORKFLOW = require_path(
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily-report.yml"
)
_BUDGET = re.compile(r'REPORT_BUDGET_S: "([0-9]+)"')
_STEP_LIMIT = re.compile(r"timeout-minutes: ([0-9]+)")
_REPORT_RUN = "bash scripts/daily-report.sh"


def _report_step_lines() -> list[str]:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    run = next(i for i, line in enumerate(lines) if _REPORT_RUN in line)
    # A step's keys sit within a few lines above its run line.
    return lines[max(0, run - 8) : run + 1]


def test_report_step_limit_is_above_the_report_budget() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    budget = _BUDGET.search(text)
    assert budget is not None, "the report step must set REPORT_BUDGET_S"
    limit = _STEP_LIMIT.search("\n".join(_report_step_lines()))
    assert limit is not None, "the report step must set its own timeout-minutes"
    assert int(limit.group(1)) * 60 > int(budget.group(1))


def test_report_step_limit_is_below_the_build_job_limit() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    build_job = text.split("  build:", 1)[1].split("  publish:", 1)[0]
    job_limit = _STEP_LIMIT.search(build_job)
    assert job_limit is not None
    step_limit = _STEP_LIMIT.search("\n".join(_report_step_lines()))
    assert step_limit is not None
    assert int(step_limit.group(1)) < int(job_limit.group(1))
