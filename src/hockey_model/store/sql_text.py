"""SQL text that several store readers share."""

import re
import sqlite3
from collections.abc import Iterable

from hockey_model.store.db import StoreError

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def statements(text: str) -> tuple[str, ...]:
    """Split a SQL script into statements.

    ``executescript`` commits before it runs, which would end the upgrade transaction, so a
    script goes through ``execute`` one statement at a time. A trailing comment is not a
    statement, and a trailing statement with no semicolon is refused rather than dropped.
    """
    found: list[str] = []
    pending: list[str] = []
    for line in text.splitlines(keepends=True):
        pending.append(line)
        joined = "".join(pending)
        if sqlite3.complete_statement(joined):
            if joined.strip():
                found.append(joined.strip())
            pending.clear()
    leftover = "".join(pending).strip()
    # SQLite accepts both comment forms, so a trailing ``/* note */`` is not a statement, and
    # a line that starts with ``--`` is a comment. Only real text is an incomplete statement.
    body = _BLOCK_COMMENT.sub("", leftover)
    if any(line.strip() and not line.lstrip().startswith("--") for line in body.splitlines()):
        raise StoreError(
            f"split SQL script: the last statement is incomplete: {leftover!r}; add the "
            "missing semicolon at the end of the script"
        )
    return tuple(found)


def placeholders(values: Iterable[object]) -> str:
    """Return one ``?`` for each value, joined by commas, for an ``IN (`` list.

    Callers that build a SQL string and a parameter tuple must expand the values in the same
    order, so the tuple for a query that names the list twice repeats it twice.
    """
    return ",".join("?" for _ in values)
