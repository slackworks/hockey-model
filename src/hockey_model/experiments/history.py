"""Git-history checks for experiment records.

Two registry rules are enforced against commits, not against the file alone:
a record is frozen at registration, and a confirmation run is one-shot.
The withdrawn rules read the same history in withdrawn.py.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from hockey_model.experiments.looks import CONFIRMATION
from hockey_model.experiments.models import (
    IMMUTABLE_FIELDS,
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
    if not _is_work_tree(repo_root) or not _has_head(repo_root):
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
    broken promise, so it is not compared. Every commit of the record is compared, not only
    the head: a run sees the record of its own commit, so a field that changed around the run
    and changed back leaves a result that the registered record did not produce.
    """
    commits = _history(record.id, repo_root)
    relative = dict(commits).get(registration_commit_sha)
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
    _check_commits_keep_fields(record.id, registered, commits, registration_commit_sha, repo_root)


def _check_commits_keep_fields(
    record_id: str,
    registered: dict[str, object],
    commits: list[tuple[str, str]],
    registration_commit_sha: str,
    repo_root: Path,
) -> None:
    """Raise when a commit between registration and head held a changed frozen field."""
    names = [name for name in IMMUTABLE_FIELDS if name in registered]
    for commit, relative in commits:
        if commit == registration_commit_sha:
            continue
        stored = _record_at(commit, relative, repo_root)
        moved = [name for name in names if stored.get(name) != registered[name]]
        if moved:
            raise HockeyModelError(
                f"experiment {record_id!r}: immutable field(s) {', '.join(moved)} differ from "
                f"registration commit {registration_commit_sha} in commit {commit}; a run in "
                "that commit saw the changed record, so a later restore does not undo it"
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


def field_history(record_id: str, field: str, repo_root: Path) -> list[tuple[str, object]]:
    """Return ``(commit, value)`` of one field at each commit that touched the record."""
    return [
        (commit, _field_at(commit, relative, field, repo_root))
        for commit, relative in _history(record_id, repo_root)
    ]


def path_in_history(relative: str, repo_root: Path) -> bool:
    """Return True when any commit touched the path. Outside a repository it is False."""
    if not _is_work_tree(repo_root) or not _has_head(repo_root):
        return False
    log = _git(["log", "--full-history", "--format=%H", "--", relative], repo_root)
    return bool(log.strip())


def history_unavailable_reason(repo_root: Path) -> str | None:
    """Return why the deletion history cannot be read, or None when it can."""
    if not _is_work_tree(repo_root):
        return "not a git work tree"
    if not _has_head(repo_root):
        return "the repository has no commit yet"
    if _git(["rev-parse", "--is-shallow-repository"], repo_root).strip() == "true":
        return "the repository is a shallow clone"
    return None


def deleted_record_ids(present: list[str], repo_root: Path) -> list[str]:
    """Return the IDs of deleted records that must stay.

    A deleted record that spent its look could hide a negative result, so a record
    with a committed result stays. A confirmation record that never ran stays too when
    the commit that deleted it held a confirmation record with a higher number: the look
    budget counts earlier records by number, so the freed number could go to a new record
    that scores games which a later look already scored (#291). A rename is a deletion of the
    old path and an addition of the new path, because the log runs without rename detection.
    """
    if history_unavailable_reason(repo_root) is not None:
        return []
    deletions = _deletions(repo_root)
    tracked = _git(["ls-tree", "--name-only", "HEAD", f"{REGISTRY_DIR_NAME}/"], repo_root).split()
    names = {record_id for _, record_id in deletions}
    names |= {Path(name).stem for name in tracked if name.endswith(".json")}
    gone = [n for n in names if n not in present and RECORD_STEM.fullmatch(n)]
    below = _deleted_below_a_confirmation(set(gone), deletions, repo_root)
    return sorted(n for n in gone if n in below or _ever_had_result(n, repo_root))


def _deletions(repo_root: Path) -> list[tuple[str, str]]:
    """Return ``(commit, record_id)`` for each deletion in every commit reachable from HEAD.

    A deletion on a side branch that a merge brought in shows up here, because a merge
    commit does not repeat it on the first-parent line (#319). Each deletion is read in its
    own commit, so ``_was_outlived`` compares it with that commit's parent. A merge shows what
    it removed from its first parent, so a deletion that only the merge made is listed too.
    """
    log = _git(
        [
            "log",
            "--diff-merges=first-parent",
            "--no-renames",
            "--diff-filter=D",
            "--name-only",
            "--format=commit:%H",
            "--",
            f"{REGISTRY_DIR_NAME}/",
        ],
        repo_root,
    )
    return parse_deletion_log(log)


def _deleted_below_a_confirmation(
    gone: set[str], deletions: list[tuple[str, str]], repo_root: Path
) -> set[str]:
    """Return the deleted confirmation records that a higher confirmation record outlived.

    Each commit that deleted a record is read with its own tree, so a deletion that
    happened before any higher record existed stays allowed.
    """
    found: set[str] = set()
    for commit, record_id in deletions:
        if record_id in gone and _was_outlived(record_id, commit, repo_root):
            found.add(record_id)
    return found


def parse_history_log(text: str) -> list[tuple[str, str]]:
    """Return ``(commit, path)`` for each file name that a commit lists.

    ``text`` is the output of ``git log --name-only --format=commit:%H``. A commit that lists no
    name, such as a merge, gives no pair. A file name before the first ``commit:`` line means that
    the output is not a log of this form, and it raises.
    """
    pairs: list[tuple[str, str]] = []
    commit: str | None = None
    for line in text.splitlines():
        name = line.strip()
        if name.startswith("commit:"):
            commit = name.removeprefix("commit:")
        elif name:
            if commit is None:
                raise HockeyModelError(
                    f"read the commit log: the file name {name!r} comes before any commit; "
                    "the log must use --format=commit:%H"
                )
            pairs.append((commit, name))
    return pairs


def parse_deletion_log(text: str) -> list[tuple[str, str]]:
    """Return ``(commit, record_id)`` for each JSON file that a commit deleted.

    ``text`` is the output of ``git log --diff-filter=D --name-only --format=commit:%H``. A
    file name before the first ``commit:`` line means that the output is not a log of this
    form, and it raises.
    """
    return [
        (commit, Path(name).stem)
        for commit, name in parse_history_log(text)
        if name.endswith(".json")
    ]


def outlived(
    record_id: str, other_ids: Iterable[str], is_confirmation: Callable[[str], bool]
) -> bool:
    """Return True when a record with a higher number than ``record_id`` is a confirmation."""
    return any(
        RECORD_STEM.fullmatch(other) is not None
        and other[:4] > record_id[:4]
        and is_confirmation(other)
        for other in other_ids
    )


def _was_outlived(record_id: str, commit: str, repo_root: Path) -> bool:
    """Return True when ``commit`` deleted a confirmation record that a higher one outlived.

    A higher record outlives the deletion when it is in the parent of the commit and in the
    commit. A record that the same commit adds is not older than the deleted one, so a squash
    merge of a branch that deletes and adds records keeps the verdict of the branch.
    """
    relative = record_relative_path(record_id)
    if _field_at(f"{commit}^", relative, "cohort", repo_root) != CONFIRMATION:
        return False
    before = set(
        _git(["ls-tree", "--name-only", f"{commit}^", f"{REGISTRY_DIR_NAME}/"], repo_root).split()
    )
    names = [
        name
        for name in _git(
            ["ls-tree", "--name-only", commit, f"{REGISTRY_DIR_NAME}/"], repo_root
        ).split()
        if name in before
    ]
    return outlived(
        record_id,
        (Path(name).stem for name in names if name.endswith(".json")),
        lambda other: (
            _field_at(commit, record_relative_path(other), "cohort", repo_root) == CONFIRMATION
        ),
    )


def _ever_had_result(record_id: str, repo_root: Path) -> bool:
    """Return True when any commit stored a result for the record.

    A commit that deleted the file has no copy of it, so it is skipped. Any git failure, and a
    stored version that is not valid JSON, raises: a skipped version could hide a result.
    """
    relative = record_relative_path(record_id)
    for commit in _git(["log", "--full-history", "--format=%H", "--", relative], repo_root).split():
        if not _git(["ls-tree", "--name-only", commit, "--", relative], repo_root).strip():
            continue
        if _field_at(commit, relative, "result", repo_root) is not None:
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

    A commit that deleted the file is left out, because it holds no copy to read.

    The history is the history of one path. It holds the commits of every branch that a merge
    brought in, because git hides a side branch from a path log when the merge keeps the other
    side. Git does not follow a rename, because it pairs a removed file with an added file by
    how alike the two are. A record that moves to another name is a deleted record and a new
    record (docs/design/experiment-registry-tla.md).
    """
    out = _git(
        [
            "log",
            "--full-history",
            # A merge commit lists its files against each parent, so its own copy is read.
            "--diff-merges=separate",
            "--diff-filter=d",
            "--name-only",
            "--format=commit:%H",
            "--",
            record_relative_path(record_id),
        ],
        repo_root,
    )
    # A merge commit is listed once for each parent, so keep the first of each pair.
    return list(dict.fromkeys(parse_history_log(out)))


def _field_at(commit: str, relative: str, field: str, repo_root: Path) -> object:
    """Return one field of a record as one commit stored it."""
    return _record_at(commit, relative, repo_root).get(field)


def _record_at(commit: str, relative: str, repo_root: Path) -> dict[str, object]:
    """Return a record as one commit stored it, as a JSON object."""
    text = _git(["show", f"{commit}:{relative}"], repo_root)
    return parse_record_object(text, f"{relative} at commit {commit}")


def parse_record_object(text: str, where: str) -> dict[str, object]:
    """Return the JSON object in ``text``. ``where`` names the source in the error."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HockeyModelError(f"read {where}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HockeyModelError(
            f"read {where}: a record must be a JSON object, got a {type(data).__name__}"
        )
    return data


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
