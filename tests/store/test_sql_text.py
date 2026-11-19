"""Property tests for the SQL script splitter that the store upgrade shares."""

import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.store import sql_text
from hockey_model.store.db import StoreError

# Each sample is one SQLite statement that a migration script can hold, including a
# semicolon inside a string literal and a doubled quote.
_STATEMENTS = st.sampled_from(
    [
        "SELECT 1;",
        "SELECT 2 AS two;",
        "CREATE TABLE t (x TEXT);",
        "CREATE VIEW v AS SELECT 1;",
        "INSERT INTO t (x) VALUES ('a''b');",
        "SELECT 'a;b';",
    ]
)


@settings(max_examples=50, deadline=None)
@given(chunks=st.lists(_STATEMENTS, min_size=1, max_size=6))
def test_a_script_splits_back_into_its_own_statements(chunks: list[str]) -> None:
    """A joined script gives back each statement, and a split of the split changes nothing."""
    found = sql_text.statements("\n".join(chunks))
    assert found == tuple(chunks)
    assert all(sqlite3.complete_statement(statement) for statement in found)
    assert sql_text.statements("\n".join(found)) == found


@settings(max_examples=25, deadline=None)
@given(statement=_STATEMENTS, lines=st.lists(_STATEMENTS, max_size=4))
def test_a_last_statement_with_no_end_is_refused(statement: str, lines: list[str]) -> None:
    """A dropped semicolon loses that change, so the split refuses the whole script."""
    text = "\n".join([*lines, statement.removesuffix(";")])
    with pytest.raises(StoreError, match="the last statement is incomplete"):
        sql_text.statements(text)
