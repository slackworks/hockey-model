#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# ///
"""Run mutation tests on the functions that the pushed commits change.

The pre-push stage calls ``run``. The check finds the merge base of HEAD with the upstream
branch, or with the remote default branch on a first push. It maps each changed line of a
covered source file (``[tool.mutmut] only_mutate``) to the top-level function or class
method that holds it, and runs mutmut on those functions only.

A surviving mutant fails the push when its mutated line is a changed line. A mutant that hangs
(status timeout) is gated the same way. A hang on a line that the push does not change is
listed in the output and does not fail the push. The check works line by line, so a change can
make a distant line hang, and the weekly run still counts every such mutant. Any other
incomplete result fails the push. So does a mutant whose mutated line the check cannot find.
A push that changes no covered function, such as a docs-only or a test-only push, runs no mutants.
A deleted file selects nothing, because its mutants no longer exist.
A renamed file counts as added at its new path.

The check gates HEAD against its tracking branch. It ignores the refs that git passes on
stdin, so it is right for one checkout that pushes its own branch. A push of another
branch from the same checkout is gated against HEAD, not against that branch.

Subcommands:

- ``plan``: print the mutant-name globs that the changed code selects, one per line.
- ``run``: run mutmut on the selected globs and gate the results.

Exit 0 on a pass or on nothing to check, 1 on a failed check, and 2 on a bad argument.
Diagnostics go to stderr.
"""

import argparse
import ast
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

PYPROJECT = "pyproject.toml"
MUTANTS_DIR = "mutants"
# A changed-code run that passes this many seconds fails with an actionable message.
MUTMUT_TIMEOUT_S = 900
MUTMUT_COMMAND = ("uv", "run", "mutmut")
# Top-level names that mutmut writes into its output directory. Any other name is refused.
MUTANTS_OUTPUT_NAMES = frozenset(
    {"mutmut-stats.json", "pyproject.toml", "src", "tests", "uv.lock", ".hypothesis"}
)
# Statuses that a mutant can carry when the tests do not kill it. The changed lines gate them.
SURVIVING = frozenset({"survived", "no tests"})
# A mutant that ran past its limit. The changed lines gate it like a survivor.
HUNG = frozenset({"timeout"})
# Statuses that show the test run did not finish. These always fail the push.
INCOMPLETE = frozenset(
    {"suspicious", "not checked", "skipped", "segfault", "check was interrupted by user"}
)
# mutmut counts a mutant that the type check rejects as detected.
PASSING = frozenset({"killed", "caught by type check"})
# mutmut joins the parts of a method name with this character and prefixes "x".
METHOD_SEPARATOR = "ǁ"
HUNK_NEW = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
HUNK_OLD = re.compile(r"^@@ -(\d+)")
RESULT_LINE = re.compile(r"^\s+(?P<name>\S+): (?P<status>.+)$")


class PushCheckError(Exception):
    """A failed check. The message names the cause and the fix."""


@dataclass(frozen=True)
class FileChange:
    """One path from ``git diff --name-status``. ``status`` is A, M, or D."""

    status: str
    path: str


@dataclass(frozen=True)
class Target:
    """A function or method that holds a changed line.

    ``first_line`` is the first decorator line, or the ``def`` line when there is none.
    mutmut numbers the lines of a mutant from it.
    """

    qualname: str
    first_line: int
    last_line: int


def _git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in ``repo`` and return the completed process without raising."""
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return stdout. Raise with git's stderr on failure."""
    done = _git_run(repo, *args)
    if done.returncode != 0:
        raise PushCheckError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def tracking_ref(repo: Path) -> str | None:
    """Return the remote branch that HEAD tracks, or None when HEAD tracks none.

    git exits 1 for "no tracking branch" and 128 for a broken repository. Only the first
    means absent.
    """
    done = _git_run(repo, "rev-parse", "--verify", "--quiet", "@{upstream}")
    if done.returncode == 0:
        return done.stdout.strip()
    if done.returncode == 1:
        return None
    raise PushCheckError(f"git rev-parse @{{upstream}} failed: {done.stderr.strip()}")


def remote_default_branch(repo: Path) -> str:
    """Return the remote default branch, such as ``origin/main``."""
    done = _git_run(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if done.returncode != 0:
        raise PushCheckError(
            "no tracking branch and no origin/HEAD, so the change base is unknown. "
            "Run `git remote set-head origin --auto`, then push again."
        )
    return done.stdout.strip().removeprefix("refs/remotes/")


def resolve_base(repo: Path) -> tuple[str, str]:
    """Return ``(merge base sha, label)`` of HEAD with its tracking branch or the default.

    A first push has no tracking branch, so the default branch is the base there. The label
    names which one was used, so the log shows the size of the check.
    """
    tracked = tracking_ref(repo)
    target, label = (
        (tracked, "tracking branch")
        if tracked
        else (
            remote_default_branch(repo),
            "default branch",
        )
    )
    return git(repo, "merge-base", "HEAD", target).strip(), label


def changed_files(repo: Path, base: str) -> list[FileChange]:
    """Return the committed path changes from ``base`` to HEAD. A rename is a delete plus an add."""
    changes: list[FileChange] = []
    for line in git(repo, "diff", "--name-status", "-M", base, "HEAD").splitlines():
        fields = line.split("\t")
        if fields[0].startswith("R"):
            changes.append(FileChange("D", fields[1]))
            changes.append(FileChange("A", fields[2]))
        else:
            changes.append(FileChange(fields[0][0], fields[1]))
    return changes


def covered_globs(repo: Path) -> list[str]:
    """Return ``[tool.mutmut] only_mutate`` from the project's pyproject.toml."""
    data = tomllib.loads((repo / PYPROJECT).read_text(encoding="utf-8"))
    try:
        return list(data["tool"]["mutmut"]["only_mutate"])
    except KeyError as exc:
        raise PushCheckError(
            f"{PYPROJECT} has no [tool.mutmut] only_mutate list ({exc}); add it, "
            "because the check needs it to find the covered modules."
        ) from exc


def is_covered(path: str, globs: list[str]) -> bool:
    """Return True when ``path`` matches one of the covered globs."""
    return any(fnmatch.fnmatchcase(path, glob) for glob in globs)


def changed_lines(repo: Path, base: str, path: str) -> set[int]:
    """Return the new-side line numbers that the diff from ``base`` to HEAD changes.

    A pure deletion has no new lines. It marks the line before the deletion instead.
    """
    return hunk_new_lines(git(repo, "diff", "-U0", base, "HEAD", "--", path))


def hunk_new_lines(diff: str) -> set[int]:
    """Return the new-side line numbers of a unified diff with no context lines."""
    lines: set[int] = set()
    for line in diff.splitlines():
        match = HUNK_NEW.match(line)
        if match is None:
            continue
        start = int(match[1])
        count = int(match[2]) if match[2] is not None else 1
        if count == 0:
            lines.add(max(start, 1))
        else:
            lines.update(range(start, start + count))
    return lines


def module_name(path: str) -> str:
    """Return the dotted mutmut module for a source path, such as ``hockey_model.sources.nhl``."""
    dotted = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return dotted.removesuffix(".__init__")


def mutmut_name(qualname: str) -> str:
    """Return the name mutmut gives a function or method, without its ``__mutmut_N`` suffix."""
    if "." not in qualname:
        return f"x_{qualname}"
    return METHOD_SEPARATOR.join(["x", *qualname.split(".")])


def mutant_glob(path: str, qualname: str) -> str:
    """Return the glob that matches every mutant of one function."""
    return f"{module_name(path)}.{mutmut_name(qualname)}__mutmut_*"


def _targets_in(node: ast.stmt, prefix: str, lines: set[int], out: list[Target]) -> None:
    """Collect the functions under ``node`` that hold a changed line. Nested functions stay
    inside their outer function's target."""
    if isinstance(node, ast.ClassDef):
        for child in node.body:
            _targets_in(child, f"{prefix}{node.name}.", lines, out)
        return
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return
    first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    last = node.end_lineno or node.lineno
    if lines & set(range(first, last + 1)):
        out.append(Target(f"{prefix}{node.name}", first, last))


def targets_at(source: str, lines: set[int]) -> list[Target]:
    """Return the top-level functions and class methods that hold a changed line."""
    out: list[Target] = []
    for node in ast.parse(source).body:
        _targets_in(node, "", lines, out)
    return out


def mutated_original_lines(diff: str) -> set[int]:
    """Return the function-relative lines that a ``mutmut show`` diff changes.

    The diff header and the lines before the first hunk carry no line numbers, so they are
    skipped. A removed line numbers the original, and an added line does not.
    """
    lines: set[int] = set()
    old: int | None = None
    for line in diff.splitlines():
        match = HUNK_OLD.match(line)
        if match is not None:
            old = int(match[1])
        elif old is None:
            continue
        elif line.startswith("-"):
            lines.add(old)
            old += 1
        elif line.startswith(" "):
            old += 1
    return lines


def parse_results(text: str) -> dict[str, str]:
    """Return ``{mutant name: status}`` from ``mutmut results``. Raise on a malformed line."""
    statuses: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = RESULT_LINE.match(line)
        if match is None:
            raise PushCheckError(f"mutmut results has a line that is not a result: {line!r}")
        name = match["name"]
        if name in statuses:
            raise PushCheckError(f"mutmut results lists mutant {name!r} twice")
        statuses[name] = match["status"]
    return statuses


def clear_generated_mutants(repo: Path) -> None:
    """Remove the mutants directory only when it holds mutmut output and nothing else."""
    target = repo / MUTANTS_DIR
    if not target.exists():
        return
    names = {entry.name for entry in target.iterdir()} if target.is_dir() else set()
    generated = {"mutmut-stats.json", PYPROJECT} <= names and names <= MUTANTS_OUTPUT_NAMES
    if not generated:
        raise PushCheckError(
            f"{MUTANTS_DIR}/ does not hold only mutmut output, so it was not removed. "
            "Move it aside, then push again."
        )
    shutil.rmtree(target)


def run_mutmut(repo: Path, args: list[str]) -> str:
    """Run one mutmut command and return its stdout.

    The command runs in its own process group. A timeout kills the whole group, so no
    worker keeps writing the mutants directory after the push fails.
    """
    proc = subprocess.Popen(
        [*MUTMUT_COMMAND, *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=MUTMUT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise PushCheckError(
            f"mutmut {args[0]} ran past {MUTMUT_TIMEOUT_S}s and was stopped. Push fewer "
            "functions per change, or raise MUTMUT_TIMEOUT_S in scripts/pre_push_mutation.py "
            "with a reason."
        ) from exc
    if proc.returncode != 0:
        raise PushCheckError(f"mutmut {' '.join(args)} exited {proc.returncode}: {stderr.strip()}")
    return stdout


def clean_worktree(repo: Path) -> None:
    """Refuse a dirty tree. mutmut reads the working tree, and the gate reads HEAD."""
    if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise PushCheckError(
            "the working tree has uncommitted changes. Commit or stash them, because the "
            "check runs mutmut on the files as they are and compares them with HEAD."
        )


def covered_targets(repo: Path, base: str) -> list[tuple[str, Target, set[int]]]:
    """Return ``(path, target, changed lines)`` for each covered function that a push changes."""
    globs = covered_globs(repo)
    found: list[tuple[str, Target, set[int]]] = []
    for change in changed_files(repo, base):
        if change.status == "D" or not is_covered(change.path, globs):
            continue
        lines = changed_lines(repo, base, change.path)
        source = git(repo, "show", f"HEAD:{change.path}")
        found.extend((change.path, target, lines) for target in targets_at(source, lines))
    return found


def _function_nodes(body: list[ast.stmt]) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return the top-level functions and class methods, without nested functions."""
    nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            nodes.extend(_function_nodes(node.body))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes.append(node)
    return nodes


def uncovered_changes(repo: Path, base: str) -> dict[str, list[int]]:
    """Return the changed lines of covered files that no function holds, by path."""
    globs = covered_globs(repo)
    found: dict[str, list[int]] = {}
    for change in changed_files(repo, base):
        if change.status == "D" or not is_covered(change.path, globs):
            continue
        source = git(repo, "show", f"HEAD:{change.path}")
        covered: set[int] = set()
        for node in _function_nodes(ast.parse(source).body):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            covered.update(range(first, (node.end_lineno or node.lineno) + 1))
        loose = sorted(changed_lines(repo, base, change.path) - covered)
        if loose:
            found[change.path] = loose
    return found


def owner_of(
    name: str, found: list[tuple[str, Target, set[int]]]
) -> tuple[str, Target, set[int]] | None:
    """Return the changed function that owns a mutant name, or None."""
    for entry in found:
        if fnmatch.fnmatchcase(name, mutant_glob(entry[0], entry[1].qualname)):
            return entry
    return None


def missing_mutants(globs: list[str], statuses: dict[str, str]) -> list[str]:
    """Name each selected function that mutmut produced no mutant for."""
    return [
        f"{glob}: mutmut has no mutant for it; check the module and function names"
        for glob in globs
        if not any(fnmatch.fnmatchcase(name, glob) for name in statuses)
    ]


def mutates_changed_line(repo: Path, name: str, owner: tuple[str, Target, set[int]]) -> bool:
    """Return True when the mutant ``name`` mutates a changed line.

    Raise when ``mutmut show`` names no removed line. The mutation then has no place, and a
    quiet False would let a hang or a survivor pass the push.
    """
    _, target, lines = owner
    relative = mutated_original_lines(run_mutmut(repo, ["show", name]))
    if not relative:
        raise PushCheckError(
            f"mutmut show {name} names no removed line, so the mutation has no place. "
            "Run that command by hand and read the diff."
        )
    absolute = {target.first_line + offset - 1 for offset in relative}
    return bool(absolute & lines)


class Verdict(NamedTuple):
    """The result for one mutant: a failure line, or ``ignored`` for a hang off the changes."""

    failure: str | None = None
    ignored: bool = False


def gate_one(repo: Path, name: str, status: str, owner: tuple[str, Target, set[int]]) -> Verdict:
    """Return the verdict for one mutant that the tests did not kill."""
    if status in INCOMPLETE:
        return Verdict(f"{name}: {status}; the run did not finish")
    if status not in SURVIVING | HUNG:
        return Verdict(f"{name}: unknown status {status!r}")
    if mutates_changed_line(repo, name, owner):
        return Verdict(f"{name}: {status} on a changed line")
    return Verdict(ignored=status in HUNG)


def gate(
    repo: Path, statuses: dict[str, str], found: list[tuple[str, Target, set[int]]]
) -> tuple[list[str], list[str]]:
    """Return one failure line per mutant that fails the push, and the hangs left alone."""
    failures: list[str] = []
    ignored: list[str] = []
    for name, status in sorted(statuses.items()):
        owner = owner_of(name, found)
        if owner is None or status in PASSING:
            continue
        verdict = gate_one(repo, name, status, owner)
        if verdict.failure is not None:
            failures.append(verdict.failure)
        if verdict.ignored:
            ignored.append(name)
    return failures, ignored


def run_check(repo: Path) -> int:
    """Run the changed-code check and return the exit code."""
    started = time.monotonic()
    base, label = resolve_base(repo)
    print(f"pre-push mutation: base {base[:12]} ({label})", file=sys.stderr)
    clean_worktree(repo)
    for path, lines in uncovered_changes(repo, base).items():
        print(
            f"pre-push mutation: not mutated, outside any function: {path} lines {lines}",
            file=sys.stderr,
        )
    found = covered_targets(repo, base)
    if not found:
        print("pre-push mutation: no covered function changed; nothing to run", file=sys.stderr)
        return 0
    globs = sorted({mutant_glob(path, target.qualname) for path, target, _ in found})
    clear_generated_mutants(repo)
    run_mutmut(repo, ["run", *globs])
    statuses = parse_results(run_mutmut(repo, ["results"]))
    gate_failures, ignored = gate(repo, statuses, found)
    failures = missing_mutants(globs, statuses) + gate_failures
    elapsed = time.monotonic() - started
    if ignored:
        print(
            f"pre-push mutation: ignored {len(ignored)} hung mutants off the changed lines: "
            + ", ".join(ignored),
            file=sys.stderr,
        )
    for failure in failures:
        print(f"pre-push mutation: {failure}", file=sys.stderr)
    if failures:
        print(
            f"pre-push mutation: FAILED after {elapsed:.0f}s; fix or test the lines above",
            file=sys.stderr,
        )
        return 1
    print(
        f"pre-push mutation: {len(found)} functions in {len(globs)} globs passed in {elapsed:.0f}s",
        file=sys.stderr,
    )
    return 0


# Every error the check raises on purpose, or on a broken input file, ends with a message.
REPORTED_ERRORS = (
    PushCheckError,
    KeyError,
    OSError,
    SyntaxError,
    ValueError,
    tomllib.TOMLDecodeError,
)


def main(argv: list[str] | None = None) -> int:
    """Parse the subcommand, run it, and return the exit code."""
    parser = argparse.ArgumentParser(description="Mutation tests on the functions a push changes.")
    parser.add_argument("command", choices=["plan", "run"])
    args = parser.parse_args(argv)
    try:
        repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        if args.command == "run":
            return run_check(repo)
        base, _ = resolve_base(repo)
        for path, target, _ in covered_targets(repo, base):
            print(mutant_glob(path, target.qualname))
        return 0
    except REPORTED_ERRORS as err:
        print(f"pre-push mutation: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
