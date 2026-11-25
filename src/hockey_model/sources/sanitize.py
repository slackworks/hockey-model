"""Remove the market fields of a source payload, so no price data reaches the store."""

from collections.abc import Mapping

# The ESPN keys that hold a betting market. The model reads no price data, so a payload loses
# these keys before it is parsed or archived (#82).
MARKET_KEYS = ("pickcenter", "odds", "againstTheSpread")


def strip_market_fields(payload: object) -> object:
    """Return a new payload with every market key removed at any depth.

    The input is not changed. A value that is not a mapping or a list is returned as it is.
    """
    if isinstance(payload, Mapping):
        return {
            key: strip_market_fields(value)
            for key, value in payload.items()
            if key not in MARKET_KEYS
        }
    if isinstance(payload, list):
        return [strip_market_fields(item) for item in payload]
    return payload
