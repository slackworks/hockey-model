"""Tests for the daily-prepare workflow: its schedule, its cache, and its force input."""

import re
from pathlib import Path

import pytest
from mutation_copy import require_path
from workflow_support import job_condition

ROOT = Path(__file__).resolve().parents[1]
PREPARE = require_path(ROOT / ".github" / "workflows" / "daily-prepare.yml")
STAND_ASIDE = require_path(ROOT / "scripts" / "prepare-stand-aside.sh")
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
    attempt = "hockey-model-store-${{ github.run_id }}-${{ github.run_attempt }}"
    assert all(key == attempt or key.startswith(f"{attempt}-") for key in keys), keys


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


@pytest.mark.parametrize("name", ["skip", "prepare"])
def test_the_store_group_is_on_each_build_job_and_not_on_the_workflow(name: str) -> None:
    """A group on the workflow would let a scheduled run cancel a waiting manual run (#285).

    The skip job writes the store through the refresh, so it takes the same group as the
    rebuild (#311).
    """
    text = PREPARE.read_text(encoding="utf-8")
    job = _job(text, name)
    assert not re.search(r"^concurrency:", text, re.MULTILINE)
    assert "group: hockey-model-store" in job
    assert "cancel-in-progress: false" in job
    assert "concurrency" not in _job(text, "gate")


def test_the_prepare_job_waits_for_the_gate_and_the_skip_check() -> None:
    """The rebuild runs only when the skip check named it, so it waits for that job (#311)."""
    text = PREPARE.read_text(encoding="utf-8")
    prepare = _job(text, "prepare")
    assert "needs: [gate, skip, hold]" in prepare
    assert "needs.gate.outputs.go == 'true'" in prepare
    assert "needs.skip.outputs.rebuild == 'true'" in prepare
    assert "needs.hold.outputs.go == 'true'" in prepare


def test_a_forced_run_rebuilds_even_though_the_skip_job_is_skipped() -> None:
    """A skipped job fails the jobs that need it, so the condition must override that (#311)."""
    condition = job_condition(PREPARE.read_text(encoding="utf-8"), "prepare")
    assert condition.index("!cancelled()") < condition.index("inputs.force == true")


def test_a_cancelled_run_does_not_start_the_rebuild() -> None:
    """A job whose `if` holds always() ignores a cancel, so a 180-minute rebuild never stops."""
    condition = job_condition(PREPARE.read_text(encoding="utf-8"), "prepare")
    assert "always()" not in condition


def test_the_force_flag_comes_from_the_dispatch_input() -> None:
    """The input reaches a job `if` directly, so one value answers every reader (FAQ: contexts).

    A gate output that copies the input is a second place to keep right. The platform reads
    the input in `jobs.<job_id>.if`, as the reference table of contexts says.
    """
    text = PREPARE.read_text(encoding="utf-8")
    assert "force: ${{ steps.manual.outputs.force }}" not in text
    assert "outputs.force" not in text


def test_the_skip_job_is_short_and_runs_only_without_force() -> None:
    """A prepared day holds the group for minutes, and a forced run rebuilds at once (#311)."""
    skip = _job(PREPARE.read_text(encoding="utf-8"), "skip")
    assert "needs: gate" in skip
    assert "hold" not in skip
    assert "timeout-minutes: 10" in skip
    assert "needs.gate.outputs.go == 'true'" in skip
    assert "inputs.force != true" in skip


def test_the_skip_job_asks_the_script_whether_the_day_needs_the_rebuild() -> None:
    """Exit code 4 names the rebuild and any other code stops the run (#311)."""
    skip = _job(PREPARE.read_text(encoding="utf-8"), "skip")
    assert "--check-only" in skip
    assert "rebuild=true" in skip
    assert "rebuild=false" in skip
    assert "exit code 4" in skip
    assert "if: always()" in skip


def test_the_skip_job_hands_its_answer_to_the_rebuild_job() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    assert "rebuild: ${{ steps.check.outputs.rebuild }}" in _job(text, "skip")
    assert "needs.skip.outputs.rebuild" in _job(text, "prepare")


@pytest.mark.parametrize("name", ["skip", "prepare"])
def test_each_build_job_saves_the_cache_under_its_own_key(name: str) -> None:
    """GitHub refuses a save under a key that another job of the run took first (#311).

    The refused save is a green step with no cache entry, so the rebuilt store would never
    be stored. The restore key matches the save key of the same job, or the store that the
    job wrote is not the store that it reads back.
    """
    job = _job(PREPARE.read_text(encoding="utf-8"), name)
    attempt = "${{ github.run_id }}-${{ github.run_attempt }}"
    assert _cache_keys(job) == {f"hockey-model-store-{attempt}-{name}"}


def _cache_keys(job: str) -> set[str]:
    """Return every cache key that one job names, from a `key:` line."""
    return {key.rstrip() for key in re.findall(r"key: (hockey-model-store-.*)", job)}


def test_the_skip_and_the_rebuild_do_not_share_a_cache_key() -> None:
    """One key for both jobs is the collision that #311 found."""
    text = PREPARE.read_text(encoding="utf-8")
    skip_keys = _cache_keys(_job(text, "skip"))
    prepare_keys = _cache_keys(_job(text, "prepare"))
    assert skip_keys and prepare_keys
    assert skip_keys.isdisjoint(prepare_keys)


def test_only_a_scheduled_run_asks_whether_a_manual_run_waits() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    gate = _job(text, "gate")
    assert "actions: read" in gate
    assert "EVENT: ${{ github.event_name }}" in gate
    assert "bash scripts/prepare-stand-aside.sh" in gate
    script = STAND_ASIDE.read_text(encoding="utf-8")
    assert script.count('"$script_dir/manual-prepare-pending.sh"') == 1
    # A manual dispatch never stands aside, so the script exits before it asks.
    assert script.index('"$EVENT" != "schedule"') < script.index('"$script_dir/manual-prepare')


def test_the_prepare_timeout_stays_on_the_prepare_job() -> None:
    """The report gate reads this timeout, and the gate job must not carry a longer one."""
    text = PREPARE.read_text(encoding="utf-8")
    assert "timeout-minutes: 180" in _job(text, "prepare")
    assert "timeout-minutes: 180" not in _job(text, "gate")


def test_a_failed_lookup_stops_the_gate_and_never_lets_the_run_go() -> None:
    script = STAND_ASIDE.read_text(encoding="utf-8")
    assert "|| true" not in script
    assert 'pending="$("$script_dir/manual-prepare-pending.sh")" || {' in script
    assert "exit 1" in script


def test_only_the_answer_no_lets_a_scheduled_run_go() -> None:
    """Any answer other than yes or no stops the run, so an unknown value never starts prepare."""
    script = STAND_ASIDE.read_text(encoding="utf-8")
    assert "\nno)\n" in script
    assert "\nyes)\n" in script
    assert "not yes or no" in script


def test_a_run_that_stands_aside_says_so_as_a_warning() -> None:
    script = STAND_ASIDE.read_text(encoding="utf-8")
    assert "::warning::A manual daily-prepare run waits or runs" in script
    assert "GITHUB_STEP_SUMMARY" in script


def test_the_hold_job_has_no_store_group_and_asks_for_a_waiting_manual_run() -> None:
    """The hold job checks for a waiting manual run without joining the group (#335)."""
    text = PREPARE.read_text(encoding="utf-8")
    hold = _job(text, "hold")
    assert "concurrency:" not in hold
    assert "bash scripts/prepare-stand-aside.sh" in hold
    # The check runs after skip, because a manual run can start while skip holds the group.
    assert "needs: [gate, skip]" in hold
    assert "needs: [gate, skip, hold]" in _job(text, "prepare")
    assert "needs.hold.outputs.go == 'true'" in _job(text, "prepare")


def test_the_hold_job_runs_only_when_a_rebuild_is_about_to_join_the_group() -> None:
    """A forced run skips the skip job, so the condition needs !cancelled() (#335)."""
    condition = job_condition(PREPARE.read_text(encoding="utf-8"), "hold")
    assert condition.index("!cancelled()") < condition.index("inputs.force == true")
    assert "needs.skip.outputs.rebuild == 'true'" in condition
