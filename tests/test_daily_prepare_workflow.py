"""Tests that the daily-prepare job timeout fits the window before the next report trigger."""

import re
from pathlib import Path

import pytest
from mutation_copy import require_path

from hockey_model.report_trigger import TRIGGER_HOURS_UTC

ROOT = Path(__file__).resolve().parents[1]
PREPARE = require_path(ROOT / ".github" / "workflows" / "daily-prepare.yml")
REPORT = require_path(ROOT / ".github" / "workflows" / "daily-report.yml")
_CRON = re.compile(r'cron: "[0-9]{1,2} ([0-9]{1,2}) \* \* \*"')
_TIMEOUT = re.compile(r"timeout-minutes: ([0-9]+)")


def window_minutes(cron_hour: int) -> int:
    """Return the minutes from ``cron_hour`` to the next hour in TRIGGER_HOURS_UTC."""
    later = [hour for hour in TRIGGER_HOURS_UTC if hour > cron_hour]
    next_hour = later[0] if later else TRIGGER_HOURS_UTC[0] + 24
    return (next_hour - cron_hour) * 60


def test_prepare_timeout_fits_the_window_before_the_next_trigger() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    cron = _CRON.search(text)
    timeout = _TIMEOUT.search(text)
    assert cron is not None, "daily-prepare must have a single daily cron"
    assert timeout is not None, "the prepare job must set timeout-minutes"
    assert int(timeout.group(1)) <= window_minutes(int(cron.group(1)))


def test_window_is_three_hours_for_the_eight_am_prepare() -> None:
    assert window_minutes(8) == 180


@pytest.mark.parametrize("workflow", [PREPARE, REPORT])
def test_the_saved_cache_keeps_no_upgrade_backup(workflow: Path) -> None:
    """The cache saves the store even after a failure, so a backup must not enter it."""
    text = workflow.read_text(encoding="utf-8")
    drop = text.find('rm -f "$RUNNER_TEMP"/hockey-model/*.bak')
    save = text.find("actions/cache/save")
    assert drop != -1, "the workflow must drop the upgrade backup before it saves the cache"
    assert save != -1 and drop < save, "the backup drop must run before the cache save"
