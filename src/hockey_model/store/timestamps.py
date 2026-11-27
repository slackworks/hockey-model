"""One text form for every time the store keeps: ISO 8601 in UTC."""

from datetime import UTC, datetime

from hockey_model.types import HockeyModelError


def iso_utc(moment: datetime) -> str:
    """Return ``moment`` as ISO 8601 UTC text. A naive time is an error."""
    if moment.tzinfo is None:
        raise HockeyModelError(f"store time {moment!r} has no timezone; pass a UTC datetime")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso_utc(text: str) -> datetime:
    """Parse text that ``iso_utc`` wrote."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HockeyModelError(
            f"read store time {text!r}: not an ISO 8601 time ({exc}); the store writes UTC"
        ) from exc
    if moment.tzinfo is None:
        raise HockeyModelError(f"read store time {text!r}: no timezone; the store writes UTC")
    return moment.astimezone(UTC)
