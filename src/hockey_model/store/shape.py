"""Read the table shape that a version stamp claims to describe.

The stamp is a number that the code writes. The shape is what the tables hold. A failed
upgrade can leave the two apart, so the upgrade reads the shape before it trusts the stamp.
"""

import sqlite3
from typing import NamedTuple


class Requirement(NamedTuple):
    """One column that a schema version added or removed, and whether it must refuse NULL.

    ``present`` is False for a column that the version dropped. Such a store must not hold it.
    """

    version: int
    table: str
    column: str
    not_null: bool
    present: bool = True

    @property
    def shortfall(self) -> str:
        """Return a phrase that states what the table lacks, or what it still holds."""
        if self.present:
            return f"{self.table} still lacks {self.column}"
        return f"{self.table} still holds {self.column}"

    @property
    def need(self) -> str:
        """Return the shape that the version needs of the table."""
        if self.present:
            return f"{self.table}.{self.column}"
        return f"{self.table} without {self.column}"


# The column that each version added, in version order. A store that lacks one of these does
# not hold that version's shape. The probe reads column names on purpose: a full schema diff
# would restate schema.sql and drift from it.
REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(2, "mp_team_games", "shot_attempts_for", False),
    Requirement(3, "fit_labels", "label", False),
    Requirement(4, "mp_team_games", "attempts_known", False),
    Requirement(5, "source_reads", "fetched_at", True),
    Requirement(5, "model_fits", "created_at", True),
    Requirement(6, "source_reads", "permanent", False),
    Requirement(7, "starter_reads", "nhl_id", False, present=False),
    Requirement(7, "injury_reads", "nhl_id", False, present=False),
    Requirement(8, "mp_team_games", "unblocked_for", False, present=False),
    Requirement(8, "mp_team_games", "unblocked_against", False, present=False),
    Requirement(9, "observations", "sha256", False),
    Requirement(10, "boxscore_revisions", "derivation", False),
    Requirement(10, "skater_game_revisions", "revision", False),
    Requirement(10, "goalie_game_revisions", "revision", False),
    Requirement(11, "pbp_events", "event_id", False),
    Requirement(11, "shifts", "row_no", False),
    Requirement(11, "espn_skater_toi", "espn_id", False),
    Requirement(11, "pbp_roster", "position", False),
    Requirement(12, "stints", "stint", False),
    Requirement(12, "event_status", "status", False),
)
# The shape before the first requirement, which is the first schema this package wrote. A store
# with no schema_version row is refused before this module runs.
_OLDEST_SHAPE = 1


def holds(conn: sqlite3.Connection, requirement: Requirement) -> bool:
    """Return True when the table has the column, with NOT NULL when the requirement asks.

    ``PRAGMA table_info`` names the column at index 1 and its NOT NULL flag at index 3. The
    table name comes from ``REQUIREMENTS``, so the string join cannot carry a second statement.
    """
    pragma = "PRAGMA table_info(" + requirement.table + ")"
    found = {row[1]: row[3] for row in conn.execute(pragma)}
    if not requirement.present:
        # A table that does not exist has no column, and it does not hold the shape either.
        return bool(found) and requirement.column not in found
    if requirement.column not in found:
        return False
    return not requirement.not_null or bool(found[requirement.column])


def earned_version(conn: sqlite3.Connection) -> tuple[int, Requirement | None]:
    """Return the version the tables earn, and the first requirement they fail.

    The second value is the column that names the gap, or None when the tables hold every
    requirement. The caller uses it to tell the operator what the store is missing.

    A version can add more than one column. When a later requirement of that version fails,
    the tables earn the version before it, not the version it names.
    """
    earned = _OLDEST_SHAPE
    for requirement in REQUIREMENTS:
        if not holds(conn, requirement):
            return min(earned, requirement.version - 1), requirement
        earned = requirement.version
    return earned, None
