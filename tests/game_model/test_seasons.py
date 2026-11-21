"""Tests for the season plans that the backtest and the experiment registry use.

Covers:
- season_of for dates on both sides of the September boundary
- confirmation_plan: first season, open end, regular season only, cutoff
- in_cohort: season range, game type, cutoff
- scored_label wording
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.game_model.seasons import (
    CONFIRMATION_START,
    REGULAR_SEASON_TYPE,
    SeasonPlan,
    confirmation_plan,
    exploration_plan,
    in_cohort,
    previous_season,
    scored_label,
    season_code,
    season_of,
    season_start_year,
)
from hockey_model.sources.nhl import GameRow


def _game(season: int, game_type: int, start: datetime) -> GameRow:
    """Build one game row for cohort tests."""
    return GameRow(1, season, game_type, start, "TOR", "BOS", "OFF", 3, 2, "REG")


class TestSeasonOf:
    """Test the season a date belongs to."""

    def test_early_season_dates_use_the_previous_year(self):
        """A game in June 2027 belongs to the 2026-27 season."""
        assert season_of(datetime(2027, 6, 1).date()) == 20262027

    def test_late_season_dates_use_the_current_year(self):
        """A game in October 2026 belongs to the 2026-27 season."""
        assert season_of(datetime(2026, 10, 1).date()) == 20262027

    def test_the_boundary_is_september(self):
        """August is the last month of the previous season, September the first of the next."""
        assert season_of(datetime(2026, 8, 31).date()) == 20252026
        assert season_of(datetime(2026, 9, 1).date()) == 20262027


class TestConfirmationPlan:
    """Test the plan a confirmation record scores."""

    def test_the_cohort_starts_at_the_confirmation_season(self):
        """A cutoff inside 2026-27 scores that season onward, not an exploration season."""
        plan = confirmation_plan(datetime(2026, 10, 31, tzinfo=UTC))
        assert plan.scored == (CONFIRMATION_START, 99_999_999)

    def test_an_early_cutoff_still_starts_at_the_confirmation_season(self):
        """The floor keeps a record with an old cutoff from scoring exploration seasons."""
        plan = confirmation_plan(datetime(2020, 1, 1, tzinfo=UTC))
        assert plan.scored[0] == CONFIRMATION_START

    def test_the_cohort_is_regular_season_only(self):
        """Playoff games are outside the confirmation cohort."""
        plan = confirmation_plan(datetime(2026, 10, 31, tzinfo=UTC))
        assert plan.game_types == (2,)

    def test_the_cutoff_is_carried_into_the_plan(self):
        """The plan keeps the record's cutoff so games before it are not scored."""
        cutoff = datetime(2026, 10, 31, tzinfo=UTC)
        assert confirmation_plan(cutoff).cutoff == cutoff

    def test_exploration_plan_covers_every_scoreable_season_through_2025_26(self):
        """Exploration scores every season through 2025-26 that an earlier season can fit on."""
        plan = exploration_plan()
        assert plan.scored == (20162017, 20252026)
        assert plan.game_types is None
        assert plan.cutoff is None

    def test_exploration_starts_after_the_first_fit_season(self):
        """The first scored season needs a season before it to fit on, so it is not 2015-16."""
        plan = exploration_plan()
        assert plan.scored[0] > plan.fit_first


class TestInCohort:
    """Test which games a plan scores."""

    def test_games_outside_the_season_range_are_out(self):
        """A season below the range is not in the cohort."""
        plan = SeasonPlan(scored=(20262027, 20262027))
        assert not in_cohort(_game(20252026, 2, datetime(2025, 11, 1, tzinfo=UTC)), plan)
        assert in_cohort(_game(20262027, 2, datetime(2026, 11, 1, tzinfo=UTC)), plan)

    def test_a_game_type_filter_excludes_playoffs(self):
        """Game type 3 is out when the plan keeps only type 2."""
        plan = SeasonPlan(scored=(20262027, 20262027), game_types=(2,))
        assert not in_cohort(_game(20262027, 3, datetime(2026, 11, 1, tzinfo=UTC)), plan)
        assert in_cohort(_game(20262027, 2, datetime(2026, 11, 1, tzinfo=UTC)), plan)

    def test_games_before_the_cutoff_are_out(self):
        """A plan with a cutoff scores only games that start after it."""
        cutoff = datetime(2026, 10, 31, tzinfo=UTC)
        plan = SeasonPlan(scored=(20262027, 20262027), cutoff=cutoff)
        assert not in_cohort(_game(20262027, 2, datetime(2026, 10, 1, tzinfo=UTC)), plan)
        assert in_cohort(_game(20262027, 2, datetime(2026, 11, 1, tzinfo=UTC)), plan)

    def test_no_cutoff_keeps_every_game_in_the_range(self):
        """A plan without a cutoff scores every game in the season range."""
        plan = SeasonPlan(scored=(20242025, 20252026))
        assert in_cohort(_game(20242025, 3, datetime(2025, 6, 1, tzinfo=UTC)), plan)


class TestScoredLabel:
    """Test the text that names the scored seasons."""

    def test_a_closed_range_is_named(self):
        """A plan with an end season says both ends."""
        assert scored_label(SeasonPlan(scored=(20242025, 20252026))) == (
            "seasons 20242025 to 20252026"
        )

    def test_an_open_end_is_named(self):
        """A confirmation plan says onward instead of a fake end season."""
        plan = confirmation_plan(datetime(2026, 10, 31, tzinfo=UTC))
        assert scored_label(plan) == "seasons from 20262027 onward"


@given(st.dates(min_value=date(2000, 1, 1), max_value=date(2098, 12, 31)))
def test_season_of_matches_the_september_rule(day: date) -> None:
    """The season id changes on 1 September and on no other day."""
    first_year = day.year if day.month >= 9 else day.year - 1
    assert season_of(day) == first_year * 10000 + first_year + 1
    assert season_of(day) == season_of(day + timedelta(days=1)) or (
        day.month == 8 and day.day == 31
    )


@given(
    season=st.integers(20152016, 20302031).filter(lambda s: s // 10000 + 1 == s % 10000),
    game_type=st.sampled_from([1, 2, 3, 4, 9, 19]),
    offset_days=st.integers(-400, 400),
)
def test_a_confirmation_cohort_never_holds_a_game_on_or_before_the_cutoff(
    season: int, game_type: int, offset_days: int
) -> None:
    """Every cohort game is a regular season game, 2026-27 or later, after the cutoff."""
    cutoff = datetime(2026, 10, 31, tzinfo=UTC)
    plan = confirmation_plan(cutoff)
    game = _game(season, game_type, cutoff + timedelta(days=offset_days))
    if in_cohort(game, plan):
        assert game.start > cutoff
        assert game.season >= CONFIRMATION_START
        assert game.game_type == REGULAR_SEASON_TYPE


@pytest.mark.parametrize(
    ("day", "start_year"),
    [
        (date(2026, 6, 30), 2025),
        (date(2026, 7, 1), 2025),
        (date(2026, 8, 31), 2025),
        (date(2026, 9, 1), 2026),
        (date(2027, 1, 9), 2026),
    ],
)
def test_season_start_year_and_code_share_one_boundary(day: date, start_year: int) -> None:
    assert season_start_year(day) == start_year
    assert season_of(day) == season_code(start_year)


def test_season_code_writes_both_years() -> None:
    assert season_code(2026) == 20262027


def test_the_previous_season_of_20252026_is_20242025() -> None:
    assert previous_season(20252026) == 20242025


@given(start=st.integers(1917, 2100))
def test_the_previous_season_is_the_season_one_year_earlier(start: int) -> None:
    assert previous_season(season_code(start)) == season_code(start - 1)
