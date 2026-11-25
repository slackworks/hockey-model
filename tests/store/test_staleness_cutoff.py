"""A gap at a cutoff comes from the reads before that cutoff only (#330)."""

from pathlib import Path

from seeding import utc

from hockey_model.sources.nhl import score_day_url
from hockey_model.store.db import connect
from hockey_model.store.source_reads import record_failed_read, record_read
from hockey_model.store.staleness import training_gaps

# A day of a season before the scored, current, and previous seasons. Its failures are
# training gaps, so ``training_gaps`` names them.
OLD_DAY = utc("2017-02-01").date()
URL = score_day_url(OLD_DAY)
FIRST_FAILURE = utc("2026-01-19T00:00")
SECOND_FAILURE = utc("2026-01-19T12:00")


def test_a_failure_after_the_cutoff_does_not_make_a_gap(tmp_path: Path) -> None:
    connection = connect(tmp_path / "store.db")
    try:
        record_failed_read(connection, "nhl_scores", URL, FIRST_FAILURE, RuntimeError("timeout"))
        record_failed_read(connection, "nhl_scores", URL, SECOND_FAILURE, RuntimeError("timeout"))
        # At 06:00 only the first failure is known, so the URL is not yet a gap.
        assert not any(URL in line for line in training_gaps(connection, utc("2026-01-19T06:00")))
        assert any(URL in line for line in training_gaps(connection, utc("2026-01-20")))
    finally:
        connection.close()


def test_a_later_good_read_does_not_clear_a_gap_at_an_earlier_cutoff(tmp_path: Path) -> None:
    connection = connect(tmp_path / "store.db")
    try:
        record_failed_read(connection, "nhl_scores", URL, FIRST_FAILURE, RuntimeError("timeout"))
        record_failed_read(connection, "nhl_scores", URL, SECOND_FAILURE, RuntimeError("timeout"))
        record_read(connection, "nhl_scores", URL, utc("2026-01-21"))
        # The good read came after the cutoff, so the gap still holds at 2026-01-20.
        assert any(URL in line for line in training_gaps(connection, utc("2026-01-20")))
        # A cutoff after the good read clears it.
        assert not any(URL in line for line in training_gaps(connection, utc("2026-01-22")))
    finally:
        connection.close()
