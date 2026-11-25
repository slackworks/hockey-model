"""One SHA-256 for a JSON value, shared by every module that names a value by its hash."""

import hashlib
import json
from typing import Any


def json_sha256(values: Any) -> str:
    """Return the SHA-256 of ``values`` as JSON with sorted keys, so key order changes nothing."""
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()
