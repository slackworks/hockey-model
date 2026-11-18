"""SQL text that several store readers share."""

import sqlite3
from collections.abc import Iterable

from hockey_model.store.db import StoreError


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
    if _has_sql(leftover):
        raise StoreError(
            f"split SQL script: the last statement is incomplete: {leftover!r}; add the "
            "missing semicolon at the end of the script"
        )
    return tuple(found)


def _has_sql(text: str) -> bool:
    """Return True when the text holds a statement rather than comments and blank lines."""
    return any(line.strip() and not line.lstrip().startswith("--") for line in text.splitlines())


def placeholders(values: Iterable[object]) -> str:
    """Return one ``?`` for each value, joined by commas, for an ``IN (`` list.

    Callers that build a SQL string and a parameter tuple must expand the values in the same
    order, so the tuple for a query that names the list twice repeats it twice.
    """
    return ",".join("?" for _ in values)
