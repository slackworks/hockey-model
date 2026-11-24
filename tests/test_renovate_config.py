"""Tests for renovate.json: the update policy of #97, and the gate on automerge."""

import json
import re
from pathlib import Path

from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = require_path(ROOT / "renovate.json")
# A person lists the days here. The issue asks for 3 to 7.
_AGE = re.compile(r"(?P<days>[0-9]+) days")
MIN_AGE_DAYS, MAX_AGE_DAYS = 3, 7


def load() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def rule(**match: object) -> dict:
    found = [r for r in load()["packageRules"] if all(r.get(k) == v for k, v in match.items())]
    assert len(found) == 1, f"expected one rule for {match}, found {len(found)}"
    return found[0]


def test_only_the_python_and_action_managers_run() -> None:
    """The pre-commit hook revision and ZIZMOR_VERSION must change in one pull request."""
    assert load()["enabledManagers"] == ["pep621", "github-actions"]


def test_a_release_waits_the_agreed_number_of_days() -> None:
    found = _AGE.fullmatch(load()["minimumReleaseAge"])
    assert found is not None, "write minimumReleaseAge as 'N days'"
    assert MIN_AGE_DAYS <= int(found.group("days")) <= MAX_AGE_DAYS


def test_internal_checks_are_strict() -> None:
    assert load()["internalChecksFilter"] == "strict"


def test_actions_are_pinned_to_digests() -> None:
    assert rule(matchManagers=["github-actions"])["pinDigests"] is True


def test_no_update_merges_by_itself_until_the_required_checks_are_known() -> None:
    """Turn this on only after #104 and the repository auto-merge setting (see the design note)."""
    config = load()
    assert config["automerge"] is False
    assert all(r.get("automerge", False) is False for r in config["packageRules"])


def test_no_rule_can_turn_automerge_on_for_a_major_update() -> None:
    """The top-level switch is false, so a major update merges only when a rule names it."""
    for entry in load()["packageRules"]:
        if entry.get("automerge") is True:
            assert "major" not in entry.get("matchUpdateTypes", ["major"])


def test_the_repository_has_no_dependabot_version_updates() -> None:
    """Two bots on one dependency open the same pull request twice."""
    assert not (ROOT / ".github" / "dependabot.yml").exists()
    assert not (ROOT / ".github" / "dependabot.yaml").exists()
