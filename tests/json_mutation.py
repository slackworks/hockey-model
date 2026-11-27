"""Draw one random edit of a JSON document, for properties of a parser that reads untrusted JSON."""

import copy
from typing import Any

from hypothesis import strategies as st

# The values that a feed fault can put where a field belongs.
REPLACEMENTS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**30), max_value=10**30),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=12),
    st.lists(st.integers(min_value=0, max_value=5), max_size=3),
    st.dictionaries(st.text(max_size=4), st.integers(min_value=0, max_value=5), max_size=3),
)


def _paths(node: Any, path: tuple = ()) -> list[tuple]:
    """Return the path of every container entry of ``node``, depth first."""
    found: list[tuple] = []
    if not isinstance(node, dict | list):
        return found
    entries = node.items() if isinstance(node, dict) else enumerate(node)
    for key, value in entries:
        found.append((*path, key))
        found.extend(_paths(value, (*path, key)))
    return found


def mutations(document: Any) -> st.SearchStrategy[Any]:
    """Return a strategy of copies of ``document``, each with one entry deleted or replaced.

    A dict entry may also be renamed. The paths come from the document, so every edit hits a
    field that exists.
    """
    paths = _paths(document)

    @st.composite
    def build(draw: st.DrawFn) -> Any:
        copied = copy.deepcopy(document)
        path = draw(st.sampled_from(paths))
        parent: Any = copied
        for step in path[:-1]:
            parent = parent[step]
        last = path[-1]
        action = draw(st.sampled_from(["delete", "replace", "rename"]))
        if action == "replace":
            parent[last] = draw(REPLACEMENTS)
        elif action == "delete" or not isinstance(parent, dict):
            del parent[last]
        else:
            parent[f"{last}_renamed"] = parent.pop(last)
        return copied

    return build()
