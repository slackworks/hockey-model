"""Helpers that read one job of a workflow file as text.

A workflow test does not run the workflow. It reads the job text and checks the rules that a
text read can hold.
"""

import re


def job_text(text: str, name: str) -> str:
    """Return the lines of one job, from its heading to the next job heading, minus comments."""
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9_-]*:\n|^[a-z]|\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, f"the workflow has no job named {name}"
    # A key in a comment is not a setting, so comment lines do not count.
    return "".join(
        line
        for line in match.group(1).splitlines(keepends=True)
        if not line.lstrip().startswith("#")
    )


def job_condition(text: str, name: str) -> str:
    """Return the job-level `if:` condition of a job as one line, without comments."""
    match = re.search(
        r"^    if: (?:>-?\n(?P<folded>(?:      .*\n)+)|(?P<inline>.*)\n)",
        job_text(text, name),
        re.MULTILINE,
    )
    assert match is not None, f"the job {name} has no job-level if"
    body = match.group("folded") or match.group("inline")
    return " ".join(body.split())


def job_env(text: str, name: str) -> str:
    """Return the job-level `env:` block of a job, or an empty string when it has none."""
    match = re.search(r"^    env:\n((?:      .*\n)+)", job_text(text, name), re.MULTILINE)
    return "" if match is None else match.group(1)
