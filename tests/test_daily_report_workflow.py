"""Tests that the daily-report workflow agrees with the trigger code."""

import re
from pathlib import Path

from mutation_copy import require_path

from hockey_model.report_trigger import TRIGGER_MINUTE_UTC

WORKFLOW = require_path(
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily-report.yml"
)
_CRON = re.compile(r'cron: "([0-9]{1,2}) \* \* \* \*"')


def _minute(workflow: Path) -> int:
    match = _CRON.search(workflow.read_text(encoding="utf-8"))
    assert match is not None, f"{workflow.name} must have an hourly cron such as '44 * * * *'"
    return int(match.group(1))


def test_the_report_runs_every_hour_at_the_trigger_minute() -> None:
    assert _minute(WORKFLOW) == TRIGGER_MINUTE_UTC


def test_prepare_runs_every_hour_in_the_gap_between_two_reports() -> None:
    prepare = require_path(WORKFLOW.with_name("daily-prepare.yml"))
    gap = (TRIGGER_MINUTE_UTC - _minute(prepare)) % 60
    assert 15 <= gap <= 45, "the prepare should sit in the middle of the report gap"


def test_a_build_that_will_publish_compares_its_inputs_with_the_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    build = text[text.index("\n  build:\n") : text.index("\n  publish:\n")]
    assert "PUBLISH: ${{ needs.gate.outputs.publish }}" in build
    assert '[[ "$PUBLISH" == "true" ]]' in build
    assert text.count("--skip-unchanged") == 1
    assert "REPORT_GH_TOKEN: ${{ github.token }}" in build
    assert "\n          GH_TOKEN:" not in build


def test_build_job_fails_loudly_on_a_store_cache_miss() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "id: restore" in text
    # A restore hit on another branch is invisible; matched-key is empty when nothing restored.
    assert "steps.restore.outputs.cache-matched-key == ''" in text


def test_build_job_sets_the_report_budget_to_ten_minutes() -> None:
    assert 'REPORT_BUDGET_S: "600"' in WORKFLOW.read_text(encoding="utf-8")


def test_an_over_budget_report_is_uploaded_and_published() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    upload = text[
        text.index("actions/upload-artifact") - 120 : text.index("actions/upload-artifact")
    ]
    assert "if: always()" in upload
    assert "needs.build.outputs.report_complete == 'true'" in text
    assert "needs.build.result" not in text
    assert "report_complete: ${{ steps.state.outputs.report_complete }}" in text


def test_the_gate_step_checks_each_output_before_it_writes_it() -> None:
    """A jq failure inside an echo argument would pass, and an empty output skips the build."""
    text = WORKFLOW.read_text(encoding="utf-8")
    gate = text[text.index("\n  gate:\n") : text.index("\n  build:\n")]
    assert 'bash scripts/gate-outputs.sh gate.json >> "$GITHUB_OUTPUT"' in gate
    # jq -e exits 1 for a false value, so no gate step may read a boolean with it.
    assert "jq -e" not in gate
    assert '"due=$(jq' not in gate


def test_the_publish_job_runs_one_at_a_time_for_a_day_and_holds_the_token_for_the_script() -> None:
    """The index is a read-modify-write, and the sync must run without the release token."""
    text = WORKFLOW.read_text(encoding="utf-8")
    publish = text[text.index("\n  publish:\n") :]
    assert "group: hockey-model-publish-${{ needs.gate.outputs.day }}" in publish
    assert "cancel-in-progress: false" in publish
    job_env = publish[publish.index("    env:\n") : publish.index("    steps:\n")]
    assert "GH_TOKEN" not in job_env
    step = publish[publish.index("      - run: bash scripts/publish-daily-report.sh") :]
    assert "GH_TOKEN: ${{ github.token }}" in step
    assert 'UV_NO_SYNC: "1"' in step
