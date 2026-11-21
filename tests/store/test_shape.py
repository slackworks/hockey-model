"""Property tests for the table-shape probe that the upgrade trusts before its stamp."""

import sqlite3

from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.store.shape import REQUIREMENTS, earned_version, holds


def _store(held: tuple[bool, ...]) -> sqlite3.Connection:
    """Build a store whose tables hold exactly the requirements that ``held`` marks."""
    conn = sqlite3.connect(":memory:")
    tables: dict[str, list[str]] = {}
    for requirement, present in zip(REQUIREMENTS, held, strict=True):
        columns = tables.setdefault(requirement.table, [])
        # A held requirement has its column, or lacks it when the version dropped the column.
        if present == requirement.present:
            rule = " NOT NULL" if requirement.not_null else ""
            columns.append(f"{requirement.column} TEXT{rule}")
    for table, columns in tables.items():
        # A table with no column still exists, so the probe finds it and reports the gap.
        rest = "".join(", " + column for column in columns)
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY{rest})")
    return conn


def _expected(held: tuple[bool, ...]) -> tuple[int, object]:
    earned = 1
    for requirement, present in zip(REQUIREMENTS, held, strict=True):
        if not present:
            return min(earned, requirement.version - 1), requirement
        earned = requirement.version
    return earned, None


@settings(max_examples=50, deadline=None)
@given(held=st.tuples(*[st.booleans() for _ in REQUIREMENTS]))
def test_the_earned_version_is_the_newest_version_whose_shape_holds(
    held: tuple[bool, ...],
) -> None:
    """A gap at any requirement earns the version before it, and names the gap it found."""
    conn = _store(held)
    try:
        assert [holds(conn, requirement) for requirement in REQUIREMENTS] == list(held)
        assert earned_version(conn) == _expected(held)
    finally:
        conn.close()


@settings(max_examples=25, deadline=None)
@given(held=st.tuples(*[st.booleans() for _ in REQUIREMENTS]))
def test_a_not_null_requirement_fails_on_a_column_that_allows_null(
    held: tuple[bool, ...],
) -> None:
    """A present column that allows NULL does not hold a requirement that asks for NOT NULL."""
    conn = _store(held)
    try:
        for requirement, present in zip(REQUIREMENTS, held, strict=True):
            if not present or not requirement.not_null:
                continue
            conn.execute(f"DROP TABLE {requirement.table}")
            conn.execute(f"CREATE TABLE {requirement.table} ({requirement.column} TEXT)")
            assert not holds(conn, requirement)
            earned, _ = earned_version(conn)
            assert earned < requirement.version
            return
    finally:
        conn.close()
