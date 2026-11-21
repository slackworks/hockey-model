"""Git-history checks for experiment records.

Three registry rules are enforced against commits, not against the file alone:
a record is frozen at registration, a confirmation run is one-shot,
and a withdrawn record stays withdrawn.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hockey_model.experiments.models import (
    IMMUTABLE_FIELDS,
    STATUS_WITHDRAWN,
    ExperimentRecord,
    record_from_text,
)
from hockey_model.experiments.registry import RECORD_STEM, REGISTRY_DIR_NAME
from hockey_model.types import HockeyModelError


def record_relative_path(record_id: str) -> str:
    """Return the path of a record inside the repository, as git sees it."""
    return f"{REGISTRY_DIR_NAME}/{record_id}.json"


def registration_commit(record_id: str, repo_root: Path) -> str | None:
    """Return the commit that first added the record, or None when git does not track it."""
    if not _is_work_tree(repo_root):
        return None
    if _git(["rev-parse", "--is-shallow-repository"], repo_root).strip() == "true":
        raise HockeyModelError(
            f"check {record_id!r}: {repo_root} is a shallow clone, so the registration "
            "commit is not visible; fetch the full history (for example fetch-depth: 0)"
        )
    commits = _history(record_id, repo_root)
    return commits[-1][0] if commits else None


def check_immutable_fields(
    record: ExperimentRecord,
    registration_commit_sha: str,
    repo_root: Path,
) -> None:
    """Raise when a field that the record carried at registration changed after it.

    A field the record did not carry at registration is a schema addition, not a
    broken promise, so it is not compared.
    """
    relative = dict(_history(record.id, repo_root)).get(registration_commit_sha)
    if relative is None:
        raise HockeyModelError(
            f"experiment {record.id!r}: commit {registration_commit_sha} is not in the "
            "history of the record"
        )
    text = _git(["show", f"{registration_commit_sha}:{relative}"], repo_root)
    prior = record_from_text(text, f"commit {registration_commit_sha} of {relative}")
    registered = json.loads(text)
    current = record.to_dict()
    before = prior.to_dict()
    changed = [
        name for name in IMMUTABLE_FIELDS if name in registered and current[name] != before[name]
    ]
    if changed:
        raise HockeyModelError(
            f"experiment {record.id!r}: immutable field(s) {', '.join(changed)} changed "
            f"after registration commit {registration_commit_sha}"
        )


def check_one_shot(record: ExperimentRecord, repo_root: Path) -> None:
    """Raise when the record's history holds a result that HEAD no longer carries.

    A confirmation run is one-shot. Deleting or replacing a committed result is a
    new experiment, so it needs a new record.
    """
    # A stored blob holds lists where the record holds tuples, so compare as JSON.
    current = json.loads(json.dumps(record.to_dict().get("result")))
    for commit, relative in _history(record.id, repo_root):
        prior = _field_at(commit, relative, "result", repo_root)
        if prior is None:
            continue
        if prior != current:
            raise HockeyModelError(
                f"experiment {record.id!r}: commit {commit} recorded a result that the "
                "record no longer holds; a confirmation run is one-shot, so a new run "
                "needs a new record"
            )


def check_withdrawn_stays(record: ExperimentRecord, repo_root: Path) -> None:
    """Raise when a commit stored the record as withdrawn and HEAD does not.

    A withdrawn record is final. A later status would hide that the hypothesis was dropped
    before it ran, so a changed plan needs a new record.
    """
    if record.status == STATUS_WITHDRAWN:
        return
    for commit, relative in _history(record.id, repo_root):
        if _field_at(commit, relative, "status", repo_root) == STATUS_WITHDRAWN:
            raise HockeyModelError(
                f"experiment {record.id!r}: commit {commit} recorded the status 'withdrawn', "
                f"so the record cannot return to {record.status!r}; register a new record"
            )


def deleted_record_ids(present: list[str], repo_root: Path) -> list[str]:
    """Return the IDs of deleted records whose history holds a result.

    A deleted record that spent its look could hide a negative result, so a record
    with a committed result stays. A record that never had a result is no loss. A
    rename is not a deletion: git follows it, and the changed ``id`` fails the
    immutability check.
    """
    if not _is_work_tree(repo_root) or not _has_head(repo_root):
        return []
    deleted = _git(
        ["log", "--diff-filter=D", "--name-only", "--format=", "--", f"{REGISTRY_DIR_NAME}/"],
        repo_root,
    ).split()
    deleted += _git(["ls-tree", "--name-only", "HEAD", f"{REGISTRY_DIR_NAME}/"], repo_root).split()
    names = {Path(name).stem for name in deleted if name.endswith(".json")}
    gone = [n for n in names if n not in present and RECORD_STEM.fullmatch(n)]
    return sorted(n for n in gone if _ever_had_result(n, repo_root))


def _ever_had_result(record_id: str, repo_root: Path) -> bool:
    """Return True when any commit stored a result for the record.

    A version that is not valid JSON cannot hold a trustworthy result, so it is skipped.
    """
    relative = record_relative_path(record_id)
    for commit in _git(["log", "--format=%H", "--", relative], repo_root).split():
        # A commit that deletes the file has no copy of it. Any other failure is an error.
        present = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}:{relative}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if present.returncode != 0:
            continue
        shown = subprocess.run(
            ["git", "show", f"{commit}:{relative}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if shown.returncode != 0:
            raise HockeyModelError(
                f"read record {record_id} at {commit}: {shown.stderr.strip()}; "
                "check the repository, then run the check again"
            )
        try:
            data = json.loads(shown.stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("result") is not None:
            return True
    return False


def _has_head(repo_root: Path) -> bool:
    """Return True when the repository has at least one commit."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=repo_root, capture_output=True
    )
    return result.returncode == 0


def _history(record_id: str, repo_root: Path) -> list[tuple[str, str]]:
    """Return ``(commit, path)`` pairs for the record, newest first.

    Git follows a rename, so ``path`` is the file name the record had at that commit.
    """
    out = _git(
        [
            "log",
            "--follow",
            "--name-only",
            "--format=commit:%H",
            "--",
            record_relative_path(record_id),
        ],
        repo_root,
    )
    pairs: list[tuple[str, str]] = []
    commit = ""
    for line in out.splitlines():
        if line.startswith("commit:"):
            commit = line.removeprefix("commit:")
        elif line.strip():
            pairs.append((commit, line.strip()))
    return pairs


def _field_at(commit: str, relative: str, field: str, repo_root: Path) -> object:
    """Return one field of a record as one commit stored it."""
    text = _git(["show", f"{commit}:{relative}"], repo_root)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HockeyModelError(f"read {relative} at commit {commit}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HockeyModelError(
            f"read {relative} at commit {commit}: a record must be a JSON object, "
            f"got a {type(data).__name__}"
        )
    return data.get(field)


def _is_work_tree(repo_root: Path) -> bool:
    """Return True when repo_root is inside a git work tree.

    A directory outside any repository is False. A missing git binary or any other
    git failure raises, so a broken git never passes as an untracked record.

    The error text is not read, because its wording changes with the git version and the
    locale. A directory under a ``.git`` entry is a repository, so a git failure there is a
    real error. A failure with no such entry is a plain directory.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise HockeyModelError(f"run git in {repo_root}: git is not installed") from exc
    if result.returncode == 0:
        return result.stdout.strip() == "true"
    if any((parent / ".git").exists() for parent in (repo_root, *repo_root.parents)):
        raise HockeyModelError(f"git rev-parse in {repo_root} failed: {result.stderr.strip()}")
    return False


def _git(args: list[str], repo_root: Path) -> str:
    """Run git in the repository and return its standard output."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise HockeyModelError(f"run git in {repo_root}: git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip()
        raise HockeyModelError(f"git {' '.join(args)} in {repo_root} failed: {detail}") from exc
    return result.stdout
