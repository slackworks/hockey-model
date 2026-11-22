"""Files that hold the feature rows of one season, so a later run reads them instead of building.

One file holds one goalie mode and one feature list of one season, and its name carries the key
of the season (``season_key``). The newest key replaces the older file of the same mode, season,
and feature list. The files of the newest feature lists of that mode and season stay, and the
files of older lists go. A file that is damaged, or whose games differ from the games of the
store, is a miss, and the next build writes it again. A file that cannot be written is a warning,
because the run does not need the cache to finish.
Design: docs/design/backtest-speed.md
"""

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from hockey_model.backtest.row_codec import decode_rows, encode_rows
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.training import TrainingRow
from hockey_model.types import HockeyModelError

CACHE_DIR_NAME = "season-rows"
# A season keeps the files of this many feature lists for each goalie mode, the newest by
# file time. The daily prepare uses the list of the backtest and the list of the fit, and the
# other places leave room for the lists of an experiment.
KEEP_FEATURE_LISTS = 6


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

    def __post_init__(self) -> None:
        # The key reads no starter read, so rows that use the reads would not follow it.
        if self.mode is GoalieMode.READS:
            raise ValueError(
                "SeasonFile: the key covers the backtest modes, not the reads mode; "
                "use GoalieMode.ACTUAL or GoalieMode.GUESS"
            )

    @property
    def _tag(self) -> str:
        # A hash of the list keeps a feature name out of the file name.
        return hashlib.sha256(json.dumps(self.names).encode()).hexdigest()[:12]

    @property
    def _slot(self) -> str:
        return f"{self.mode.value}-{self.season}-{self._tag}"

    @property
    def path(self) -> Path:
        """Return the path of the file for this key."""
        return self.directory / f"{self._slot}-{self.key}.json"

    def load(self, game_ids: Sequence[int]) -> list[TrainingRow] | None:
        """Return the cached rows, or ``None`` when there is no usable file.

        ``game_ids`` are the final games of the season in start order. A file that holds other
        games, or the same games in another order, is not used.
        """
        if self.path.is_symlink():
            _warn(
                f"the cached rows {str(self.path)!r} are a link and are not read; "
                "the season is built again and the file is written again"
            )
            return None
        try:
            rows = decode_rows(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, HockeyModelError) as exc:
            _warn(
                f"the cached rows in {str(self.path)!r} cannot be read ({exc}); "
                "the season is built again and the file is written again"
            )
            return None
        if not self._matches(rows, game_ids):
            _warn(
                f"the cached rows in {str(self.path)!r} do not hold the games of season "
                f"{self.season} in the store; the season is built again and the file is "
                "written again"
            )
            return None
        self._mark_used()
        return rows

    def _mark_used(self) -> None:
        """Set the file time to now, so the lists that a run reads outlast the lists it does not."""
        try:
            os.utime(self.path)
        except FileNotFoundError:
            return  # another run removed the file after this run read it
        except OSError as exc:
            _warn(
                f"cannot mark the cached rows {str(self.path)!r} as used ({exc}); check the owner"
            )

    def _matches(self, rows: list[TrainingRow], game_ids: Sequence[int]) -> bool:
        return [r.game.game_id for r in rows] == list(game_ids) and all(
            r.game.season == self.season and r.features.game_id == r.game.game_id for r in rows
        )

    def save(self, rows: list[TrainingRow]) -> None:
        """Write the rows, and remove the older files of the same mode, season, and features.

        The file is complete or absent: it is written under a temporary name that no link can
        occupy, and then renamed.
        """
        try:
            text = encode_rows(rows)
        except (ValueError, HockeyModelError) as exc:
            _warn(
                f"cannot encode the rows of {self.path.name!r} ({exc}); the next run builds "
                "the season again; fix the value that the message names"
            )
            return
        try:
            self._write(text)
        except OSError as exc:
            _warn(
                f"cannot write the cached rows {str(self.path)!r} ({exc}); the next run builds "
                "the season again; check that the directory is writable and has free space"
            )
            return
        self._remove_older()
        self._remove_other_lists()

    def _write(self, text: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=self.directory, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            Path(name).replace(self.path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def _remove_other_lists(self) -> None:
        """Remove the files of the older feature lists of this mode and season.

        The list of this file counts as the newest, and the other lists rank by file time.
        """
        newest: dict[str, float] = {}
        paths: dict[str, list[Path]] = {}
        for path in self.directory.glob(f"{self.mode.value}-{self.season}-*-*.json"):
            tag = path.name.split("-")[2]
            try:
                # lstat, so a link that points nowhere ranks by its own time and is removed.
                newest[tag] = max(newest.get(tag, 0.0), path.lstat().st_mtime)
            except FileNotFoundError:
                continue  # another run removed the file after the listing
            paths.setdefault(tag, []).append(path)
        # Lists with the same file time rank by tag, so the result does not follow the listing.
        ranked = sorted(newest, key=lambda tag: (tag != self._tag, -newest[tag], tag))
        for tag in ranked[KEEP_FEATURE_LISTS:]:
            for old in paths[tag]:
                self._remove(old)

    def _remove(self, old: Path) -> None:
        try:
            old.unlink(missing_ok=True)
        except OSError as exc:
            _warn(f"cannot remove the old cached rows {str(old)!r} ({exc}); remove it")

    def _remove_older(self) -> None:
        """Remove the files of the same slot under another key, and the temporary files."""
        for pattern in (f"{self._slot}-*.json", f".{self._slot}-*.tmp"):
            for old in self.directory.glob(pattern):
                # A temporary file of this exact name belongs to a writer that is still at work.
                if old == self.path or old.name.startswith(f".{self.path.name}."):
                    continue
                self._remove(old)
