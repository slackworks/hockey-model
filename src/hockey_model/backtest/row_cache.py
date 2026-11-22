"""Files that hold the feature rows of one season, so a later run reads them instead of building.

One file holds one goalie mode and one feature list of one season, and its name carries the key
of the season (``season_key``). The newest key replaces the older file of the same mode, season,
and feature list, and the files of another feature list stay. A file that is damaged is a miss,
and the next build writes it again. A file that cannot be written is a warning, because the run
does not need the cache to finish.
Design: docs/design/backtest-speed.md
"""

import contextlib
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from hockey_model.backtest.row_codec import decode_rows, encode_rows
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.training import TrainingRow
from hockey_model.types import HockeyModelError

CACHE_DIR_NAME = "season-rows"


def cache_dir(database: Path) -> Path:
    """Return the directory of the season row cache of the store file ``database``.

    The directory is beside the store file, so whatever keeps the store between runs keeps it.
    """
    return database.parent / CACHE_DIR_NAME


def _warn(text: str) -> None:
    print(f"backtest: {text}", file=sys.stderr)


@dataclass(frozen=True, slots=True)
class SeasonFile:
    """The cache file of one goalie mode and feature list of one season, under one key."""

    directory: Path
    mode: GoalieMode
    season: int
    names: tuple[str, ...]
    key: str

    @property
    def _slot(self) -> str:
        # A hash of the list keeps a feature name out of the file name.
        tag = hashlib.sha256(json.dumps(self.names).encode()).hexdigest()[:12]
        return f"{self.mode.value}-{self.season}-{tag}"

    @property
    def path(self) -> Path:
        """Return the path of the file for this key."""
        return self.directory / f"{self._slot}-{self.key}.json"

    def load(self) -> list[TrainingRow] | None:
        """Return the cached rows, or ``None`` when there is no usable file."""
        try:
            return decode_rows(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, HockeyModelError) as exc:
            _warn(
                f"the cached rows in {str(self.path)!r} cannot be read ({exc}); "
                "the season is built again and the file is written again"
            )
            return None

    def save(self, rows: list[TrainingRow]) -> None:
        """Write the rows, and remove the older files of the same mode, season, and features.

        The file is complete or absent: it is written under a temporary name and then renamed.
        """
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            text = encode_rows(rows)
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self.path)
            for old in self.directory.glob(f"{self._slot}-*.json"):
                if old != self.path:
                    old.unlink()
        except (OSError, ValueError) as exc:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            _warn(
                f"cannot write the cached rows {str(self.path)!r} ({exc}); the next run builds "
                "the season again; check that the directory is writable and has free space"
            )
