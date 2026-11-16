"""The errors of fit selection and of the read-only consumer API (#80).

Every class here is a name and a docstring, so the module is one table of names and has no
job of its own. `[tool.module-budget] exclude` in `pyproject.toml` names this file, because
the value-class exemption does not cover an exception class and these five names belong
together: a caller imports them from one place, and no module holds more than the two it
raises.
"""

from hockey_model.types import HockeyModelError


class StoreMissingError(HockeyModelError):
    """Raised when the store path is absent or is not a file. The opener never creates one."""


class StoreVersionError(HockeyModelError):
    """Raised when the store schema is not the one this package reads."""


class UnknownFitError(HockeyModelError):
    """Raised when a fit id or a label names no saved fit."""


class UnknownGameError(HockeyModelError):
    """Raised when a game id is not in the store."""


class UnsupportedCapabilityError(HockeyModelError):
    """Raised when the model cannot answer a request at all.

    A missing input is never this error. It is a ``MissingInput`` in the result.
    """
