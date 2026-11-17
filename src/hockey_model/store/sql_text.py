"""SQL text that several store readers share."""

from collections.abc import Sequence


def placeholders(values: Sequence[object]) -> str:
    """Return one ``?`` for each value, joined by commas, for an ``IN (`` list.

    Callers that build a SQL string and a parameter tuple must expand the values in the same
    order, so the tuple for a query that names the list twice repeats it twice.
    """
    return ",".join("?" for _ in values)
