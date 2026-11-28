"""Errors raised when a source value or response shape is not one the parsers know."""

from hockey_model.types import HockeyModelError


class SourceShapeError(HockeyModelError):
    """Raised when a source writes a team code that the maps do not know."""

    def __init__(self, source: str, value: str) -> None:
        self.source = source
        self.value = value
        super().__init__(
            f"map {source} team code {value!r}: unknown code; add it to sources/teams.py"
        )


class SourceParseError(HockeyModelError):
    """Raised when a source response parses as data but a field is missing or has the wrong type."""

    def __init__(self, source: str, detail: str) -> None:
        self.source = source
        self.detail = detail
        super().__init__(f"read {source} data: {detail}")


class SourcePendingError(SourceParseError):
    """Raised when a source answers for a resource that it has not published yet.

    The play-by-play of a game that has not started and the shift chart of a game that just ended
    are such cases. The next read can succeed, so the failure must not count toward a known gap.
    """
