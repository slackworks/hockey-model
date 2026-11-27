"""Draw one random edit of a JSON document, for properties of a parser that reads untrusted JSON."""

import copy
from collections.abc import Iterator
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


def _generic_paths(
    node: Any, real: tuple = (), generic: tuple = ()
) -> Iterator[tuple[tuple, tuple]]:
    """Yield ``(real path, generic path)`` for each entry. A list of containers has one index."""
    if isinstance(node, dict):
        entries = [(key, key) for key in node]
    elif isinstance(node, list):
        flat = not any(isinstance(item, dict | list) for item in node)
        entries = [(i, i if flat else "*") for i in range(len(node))]
    else:
        return
    for key, name in entries:
        yield (*real, key), (*generic, name)
        yield from _generic_paths(node[key], (*real, key), (*generic, name))


FAULTS: dict[str, Any] = {"null": None, "text": "x", "negative": -1, "huge": 2**63, "empty": []}


def sweep(document: Any) -> Iterator[Any]:
    """Yield a copy of ``document`` for each distinct field path and each fault.

    A fault deletes the field or puts a bad value where it belongs. The first faults replace the
    whole document. Many entries of one list share a generic path, so the sweep visits the first
    one only.
    """
    yield from FAULTS.values()
    seen: set[tuple] = set()
    for real, generic in _generic_paths(document):
        if generic in seen:
            continue
        seen.add(generic)
        for name in ("delete", *FAULTS):
            copied = copy.deepcopy(document)
            parent: Any = copied
            for step in real[:-1]:
                parent = parent[step]
            if name == "delete":
                del parent[real[-1]]
            else:
                parent[real[-1]] = FAULTS[name]
            yield copied


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
