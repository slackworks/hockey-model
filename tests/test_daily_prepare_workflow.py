"""Tests for the daily-prepare workflow: its schedule, its cache, and its force input."""

import re
from pathlib import Path

import pytest
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
PREPARE = require_path(ROOT / ".github" / "workflows" / "daily-prepare.yml")
REPORT = require_path(ROOT / ".github" / "workflows" / "daily-report.yml")
_CRON = re.compile(r'cron: "[0-9]{1,2} \* \* \* \*"')


def test_prepare_has_one_hourly_cron() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    assert len(_CRON.findall(text)) == 1, "daily-prepare must have a single hourly cron"


@pytest.mark.parametrize("workflow", [PREPARE, REPORT])
def test_the_saved_cache_keeps_no_upgrade_backup(workflow: Path) -> None:
    """The cache saves the store even after a failure, so a backup must not enter it."""
    text = workflow.read_text(encoding="utf-8")
    drop = text.find('rm -f "$RUNNER_TEMP"/hockey-model/*.bak*')
    save = text.find("actions/cache/save")
    assert drop != -1, "the workflow must drop the upgrade backup before it saves the cache"
    assert save != -1 and drop < save, "the backup drop must run before the cache save"


CHECK = require_path(ROOT / ".github" / "workflows" / "daily-report-check.yml")
_STORE_KEY = re.compile(r"^\s*key: (hockey-model-store-.*)$", re.MULTILINE)


@pytest.mark.parametrize("workflow", [PREPARE, REPORT, CHECK])
def test_each_attempt_of_a_run_has_its_own_store_key(workflow: Path) -> None:
    """A re-run keeps its run_id, so a key of the run_id alone restores the store of attempt 1."""
    keys = _STORE_KEY.findall(workflow.read_text(encoding="utf-8"))
    assert keys
    assert set(keys) == {"hockey-model-store-${{ github.run_id }}-${{ github.run_attempt }}"}


def test_only_a_manual_dispatch_can_force_a_rebuild() -> None:
    """The force flag comes from a dispatch input, so a scheduled run never rebuilds a day."""
    text = PREPARE.read_text(encoding="utf-8")
    assert "force:\n        description:" in text
    assert "type: boolean" in text
    assert "DISPATCH_FORCE: ${{ inputs.force }}" in text
    assert '[[ "$DISPATCH_FORCE" == "true" ]]' in text
    assert "--force" in text
    assert text.count("--force") == 1


def _job(text: str, name: str) -> str:
    """Return the text of one job: from its heading to the next job heading or the end."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", text, re.DOTALL | re.MULTILINE)
    assert match is not None, f"the workflow has no job named {name}"
    # A key in a comment is not a setting, so comment lines do not count.
    return "".join(
        line
        for line in match.group(1).splitlines(keepends=True)
        if not line.lstrip().startswith("#")
    )


def test_the_store_group_is_on_the_prepare_job_and_not_on_the_workflow() -> None:
    """A group on the workflow would let a scheduled run cancel a waiting manual run (#285)."""
    text = PREPARE.read_text(encoding="utf-8")
    assert not re.search(r"^concurrency:", text, re.MULTILINE)
    assert "group: hockey-model-store" in _job(text, "prepare")
    assert "cancel-in-progress: false" in _job(text, "prepare")
    assert "concurrency" not in _job(text, "gate")


def test_the_prepare_job_waits_for_the_gate() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    prepare = _job(text, "prepare")
    assert "needs: gate" in prepare
    assert "if: needs.gate.outputs.go == 'true'" in prepare


def test_only_a_scheduled_run_asks_whether_a_manual_run_waits() -> None:
    gate = _job(PREPARE.read_text(encoding="utf-8"), "gate")
    assert "actions: read" in gate
    assert 'if [[ "$EVENT" != "schedule" ]]' in gate
    assert "EVENT: ${{ github.event_name }}" in gate
    assert gate.count("scripts/manual-prepare-pending.sh") == 1
    # A manual dispatch never stands aside, so the gate exits before it asks.
    assert gate.index('"$EVENT" != "schedule"') < gate.index("manual-prepare-pending.sh")


def test_the_prepare_timeout_stays_on_the_prepare_job() -> None:
    """The report gate reads this timeout, and the gate job must not carry a longer one."""
    text = PREPARE.read_text(encoding="utf-8")
    assert "timeout-minutes: 180" in _job(text, "prepare")
    assert "timeout-minutes: 180" not in _job(text, "gate")


def test_a_failed_lookup_stops_the_gate_and_never_lets_the_run_go() -> None:
    gate = _job(PREPARE.read_text(encoding="utf-8"), "gate")
    assert "|| true" not in gate
    assert 'pending="$(bash scripts/manual-prepare-pending.sh)" || {' in gate
    assert "exit 1" in gate


def test_a_run_that_stands_aside_says_so_as_a_warning() -> None:
    gate = _job(PREPARE.read_text(encoding="utf-8"), "gate")
    assert "::warning::A manual daily-prepare run waits or runs" in gate
