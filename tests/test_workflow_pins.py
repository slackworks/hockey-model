"""Tests that a tool version pinned in two places is the same in both."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
HOOKS = ROOT / ".pre-commit-config.yaml"


def _hook_version(repo_name: str) -> str:
    """Return the version in the ``frozen`` comment of the hook rev for ``repo_name``."""
    text = HOOKS.read_text(encoding="utf-8")
    match = re.search(rf"{re.escape(repo_name)}\n\s+rev: [0-9a-f]{{40}} # frozen: v([0-9.]+)", text)
    assert match, f"{HOOKS.name} has no pinned hook for {repo_name}"
    return match.group(1)


def _ci_version(variable: str) -> str:
    text = CI.read_text(encoding="utf-8")
    match = re.search(rf"{variable}[=:] ?v?([0-9.]+)", text)
    assert match, f"{CI.name} does not set {variable}"
    return match.group(1)


def test_the_zizmor_version_in_ci_equals_the_hook_version() -> None:
    assert _ci_version("ZIZMOR_VERSION") == _hook_version("zizmor-pre-commit")


def test_the_editorconfig_checker_version_in_ci_equals_the_hook_version() -> None:
    assert _ci_version("EC_VERSION") == _hook_version("editorconfig-checker")
