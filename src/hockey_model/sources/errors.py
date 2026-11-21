"""The error raised when an external source cannot be fetched."""

from hockey_model.types import HockeyModelError


class SourceError(HockeyModelError):
    """Raised when one external source cannot be fetched or read."""

    def __init__(
        self,
        source: str,
        url: str,
        reason: str,
        *,
        permanent: bool = False,
        status: int | None = None,
    ) -> None:
        self.source = source
        self.url = url
        self.reason = reason
        # True for an HTTP status that a retry cannot change, such as 403. The store counts a
        # streak of these as a known gap without waiting for the span rule (#279).
        self.permanent = permanent
        # The HTTP status of a refused request, or None when the failure has no status.
        self.status = status
        super().__init__(f"fetch {source} {url}: {reason}")
