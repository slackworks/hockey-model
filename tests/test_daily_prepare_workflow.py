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
    drop = text.find('rm -f "$RUNNER_TEMP"/hockey-model/*.bak')
    save = text.find("actions/cache/save")
    assert drop != -1, "the workflow must drop the upgrade backup before it saves the cache"
    assert save != -1 and drop < save, "the backup drop must run before the cache save"


def test_only_a_manual_dispatch_can_force_a_rebuild() -> None:
    """The force flag comes from a dispatch input, so a scheduled run never rebuilds a day."""
    text = PREPARE.read_text(encoding="utf-8")
    assert "force:\n        description:" in text
    assert "type: boolean" in text
    assert "DISPATCH_FORCE: ${{ inputs.force }}" in text
    assert '[[ "$DISPATCH_FORCE" == "true" ]]' in text
    assert "--force" in text
    assert text.count("--force") == 1
