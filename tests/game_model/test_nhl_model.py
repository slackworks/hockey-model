"""Tests for the NHL game model."""

import math
import sqlite3
import urllib.request
from datetime import timedelta

import pytest
from seeding import FETCHED, add_game, add_goalie, add_goalie_form, add_team_form, artifact, utc

from hockey_model.game import (
    Game,
    MissingKind,
    TrainingInfo,
    TrainingMembership,
    game_win_probability,
)
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.logistic import ELO_ONLY, LogisticFit
from hockey_model.game_model.nhl_model import LEAGUE_GOALS, NhlGameModel
from hockey_model.sources.moneypuck import MONEYPUCK_CREDIT, TEAM_GAMES_URL, goalie_games_url
from hockey_model.store.source_reads import KNOWN_SOURCES, record_read

START = utc("2026-01-20T00:00")
AS_OF = utc("2026-01-19T16:00")
GAME = Game(100, "TOR", "BOS", START)


def model(store: sqlite3.Connection, fit: LogisticFit = ELO_ONLY) -> NhlGameModel:
    return NhlGameModel(store, artifact(fit), GoalieMode.GUESS)


def season(store: sqlite3.Connection) -> None:
    for day in (15, 16, 17):
        add_game(store, day, f"2026-01-{day}T00:00", home="TOR", away="MTL")
        add_game(store, day + 100, f"2026-01-{day}T00:00", home="MTL", away="BOS")
        add_team_form(store, day, "TOR", 3.5, 2.0)
        add_team_form(store, day + 100, "BOS", 2.0, 3.0)


def test_nhl_model_sums_to_one(store: sqlite3.Connection) -> None:
    season(store)
    result = model(store).predict(GAME, AS_OF, 0.8)
    assert result.home_win + result.away_win == pytest.approx(1.0, abs=1e-12)
    assert 0.0 < result.home_win < 1.0


def test_with_no_history_the_elo_only_model_favors_home_a_little(store: sqlite3.Connection) -> None:
    result = model(store).predict(GAME, AS_OF, 0.8)
    assert result.home_win == pytest.approx(1 / (1 + 10 ** (-28 / 400)), abs=1e-12)


def test_the_fit_weights_change_the_result(store: sqlite3.Connection) -> None:
    season(store)
    plain = model(store).predict(GAME, AS_OF, 0.8).home_win
    boosted = LogisticFit(("xg_form",), 0.0, 1.0, {"xg_form": 4.0})
    assert model(store, boosted).predict(GAME, AS_OF, 0.8).home_win > plain


def test_nhl_model_credits_moneypuck(store: sqlite3.Connection) -> None:
    season(store)
    assert model(store).predict(GAME, AS_OF, 0.8).credits == (MONEYPUCK_CREDIT,)


def test_no_credit_without_moneypuck_rows(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-15T00:00")
    assert model(store).predict(GAME, AS_OF, 0.8).credits == ()


def read_every_source(store: sqlite3.Connection, when: str) -> None:
    for source in KNOWN_SOURCES - {"moneypuck"}:
        record_read(store, source, "u", utc(when))
    # The current MoneyPuck files are named, so a missing one is reported.
    for url in (TEAM_GAMES_URL, goalie_games_url(2025)):
        record_read(store, "moneypuck", url, utc(when))


def stale_notes(result) -> list[str]:
    return [m.note for m in result.missing if m.kind is MissingKind.STALE_SOURCE]


def test_nhl_model_reports_stale_source(store: sqlite3.Connection) -> None:
    season(store)
    read_every_source(store, "2026-01-19T10:00")
    record_read(store, "espn_starters", "u", utc("2026-01-19T12:00"), error="HTTP 503")
    notes = stale_notes(model(store).predict(GAME, AS_OF, 0.8))
    assert notes == ["espn_starters: the newest read of u failed"]


def test_a_later_good_read_clears_the_stale_flag(store: sqlite3.Connection) -> None:
    read_every_source(store, "2026-01-19T10:00")
    record_read(store, "espn_starters", "u", utc("2026-01-19T12:00"), error="HTTP 503")
    record_read(store, "espn_starters", "u", utc("2026-01-19T13:00"))
    assert stale_notes(model(store).predict(GAME, AS_OF, 0.8)) == []


def test_a_source_that_was_never_read_is_reported(store: sqlite3.Connection) -> None:
    notes = stale_notes(model(store).predict(GAME, AS_OF, 0.8))
    assert len(notes) == len(KNOWN_SOURCES)
    assert "moneypuck: no read of it has worked yet" in notes


def test_a_failure_alone_is_not_a_good_read(store: sqlite3.Connection) -> None:
    read_every_source(store, "2026-01-19T10:00")
    record_read(store, "moneypuck", "u2", utc("2026-01-19T14:00"), error="HTTP 503")
    assert stale_notes(model(store).predict(GAME, AS_OF, 0.8)) == [
        "moneypuck: the newest read of u2 failed"
    ]


def test_a_clamped_goal_rate_is_named(store: sqlite3.Connection) -> None:
    # MoneyPuck rows of 0.0 make every rate zero, so each side is raised to the floor.
    for day in (15, 16):
        add_game(store, day, f"2026-01-{day}T00:00", home="TOR", away="MTL", score=(1, 0))
        add_game(store, day + 100, f"2026-01-{day}T00:00", home="MTL", away="BOS", score=(1, 0))
        for team in ("TOR", "MTL"):
            add_team_form(store, day, team, 0.0, 0.0)
        for team in ("MTL", "BOS"):
            add_team_form(store, day + 100, team, 0.0, 0.0)
    notes = [m.note for m in model(store).predict(GAME, AS_OF, 0.8).missing]
    assert sum("are raised" in note for note in notes) == 2


def test_nhl_model_makes_no_network_call(
    store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    season(store)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the model made a network request")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert model(store).predict(GAME, AS_OF, 0.8).home_win > 0


def test_expected_goals_use_moneypuck_attack_and_defence(store: sqlite3.Connection) -> None:
    season(store)
    result = model(store).predict(GAME, AS_OF, 0.8)
    assert result.home_goals.point == pytest.approx((3.5 + 3.0) / 2)
    assert result.away_goals.point == pytest.approx((2.0 + 2.0) / 2)


def test_expected_goals_fall_back_to_actual_goals(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-15T00:00", home="TOR", away="MTL")
    add_game(store, 2, "2026-01-16T00:00", home="MTL", away="BOS")
    result = model(store).predict(GAME, AS_OF, 0.8)
    assert result.home_goals.point == pytest.approx((3 + 3) / 2)
    assert result.away_goals.point == pytest.approx((2 + 2) / 2)


def test_expected_goals_without_any_game_use_the_league_average(store: sqlite3.Connection) -> None:
    result = model(store).predict(GAME, AS_OF, 0.8)
    assert result.home_goals.point == LEAGUE_GOALS
    assert [m.kind for m in result.missing].count(MissingKind.XG_FORM) >= 2


def test_goal_interval_is_poisson_and_contains_the_point(store: sqlite3.Connection) -> None:
    season(store)
    wide = model(store).predict(GAME, AS_OF, 0.95).home_goals
    narrow = model(store).predict(GAME, AS_OF, 0.5).home_goals
    assert wide.low <= narrow.low <= narrow.point <= narrow.high <= wide.high
    assert wide.high - wide.low > narrow.high - narrow.low
    assert wide.level == 0.95


def test_goal_interval_matches_poisson_quantiles(store: sqlite3.Connection) -> None:
    season(store)
    projection = model(store).predict(GAME, AS_OF, 0.8).home_goals
    rate = projection.point
    cdf = 0.0
    low, high = 0, 0
    found_low = found_high = False
    for k in range(40):
        cdf += math.exp(-rate) * rate**k / math.factorial(k)
        if not found_low and cdf >= 0.1:
            low, found_low = k, True
        if not found_high and cdf >= 0.9:
            high, found_high = k, True
    assert (projection.low, projection.high) == (min(low, rate), max(high, rate))


def test_a_goalie_feature_uses_the_guessed_starter(store: sqlite3.Connection) -> None:
    season(store)
    for day in (15, 16, 17):
        add_goalie(store, day, "TOR", 11, "C. Ingram")
        add_goalie_form(store, day, 11, 3.0, 0)
        # The away goalie has a neutral form, so the home goalie is the whole difference.
        add_goalie(store, day, "BOS", 21, "B. Away")
        add_goalie_form(store, day, 21, 0.0, 0)
    fit = LogisticFit(("goalie_gsax",), 0.0, 1.0, {"goalie_gsax": 1.0})
    with_goalie = model(store, fit).predict(GAME, AS_OF, 0.8)
    without = model(store).predict(GAME, AS_OF, 0.8)
    assert with_goalie.home_win > without.home_win


def test_game_win_probability_flags_a_late_prediction(store: sqlite3.Connection) -> None:
    season(store)
    late = START + timedelta(hours=1)
    result = game_win_probability(GAME, model(store), as_of=late)
    assert result.made_after_start
    assert MissingKind.LATE_RUN in [m.kind for m in result.missing]
    on_time = game_win_probability(GAME, model(store), as_of=AS_OF)
    assert not on_time.made_after_start


def test_a_late_prediction_does_not_use_the_game_result(store: sqlite3.Connection) -> None:
    season(store)
    pregame = model(store).predict(GAME, AS_OF, 0.8)
    add_game(store, 100, "2026-01-20T00:00", fetched_at=FETCHED)
    late = model(store).predict(GAME, START + timedelta(hours=6), 0.8)
    assert late.home_win == pytest.approx(pregame.home_win)


def test_expected_goals_from_actual_goals_say_so(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-15T00:00", home="TOR", away="MTL")
    add_game(store, 2, "2026-01-16T00:00", home="MTL", away="BOS")
    notes = [m.note for m in model(store).predict(GAME, AS_OF, 0.8).missing]
    assert sum("from actual goals" in note for note in notes) == 2


def test_the_prediction_elo_starts_at_the_burn_in_season(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2013-11-01T00:00", season=20132014, score=(9, 0))
    add_game(store, 2, "2014-11-01T00:00", season=20142015, home="MTL", away="OTT")
    baseline = model(store).predict(GAME, AS_OF, 0.8).home_win
    assert baseline == pytest.approx(1 / (1 + 10 ** (-28 / 400)))


def test_a_fit_that_uses_attempt_form_predicts(store: sqlite3.Connection) -> None:
    """The model reads the form feature that the loaded fit names, not the default set."""
    add_game(store, 1, "2026-01-15T00:00", home="TOR", away="MTL")
    add_game(store, 2, "2026-01-16T00:00", home="MTL", away="BOS")
    add_team_form(store, 1, "TOR", 3.0, 2.0, attempts=(70, 30))
    add_team_form(store, 2, "BOS", 3.0, 2.0, attempts=(30, 70))
    plain = LogisticFit(("attempt_form",), 0.0, 1.0, {"attempt_form": 0.0})
    boosted = LogisticFit(("attempt_form",), 0.0, 1.0, {"attempt_form": 4.0})
    base = model(store, plain).predict(GAME, AS_OF, 0.8).home_win
    assert model(store, boosted).predict(GAME, AS_OF, 0.8).home_win > base


def test_a_fit_without_a_form_feature_reads_no_form(store: sqlite3.Connection) -> None:
    """A fit that names no form feature reports no form gap, since it never reads one."""
    season(store)
    fit = LogisticFit(("rest",), 0.0, 1.0, {"rest": 0.1})
    result = model(store, fit).predict(GAME, AS_OF, 0.8)
    kinds = {note.kind for note in result.missing}
    assert MissingKind.ATTEMPT_FORM not in kinds


def _goalie_game(store: sqlite3.Connection) -> None:
    """A game with MoneyPuck team form and one goalie who has no MoneyPuck rows."""
    season(store)
    add_goalie(store, 15, "TOR", 31, "A. Goalie")


def test_a_fit_without_the_goalie_feature_drops_the_goalie_form_notes(
    store: sqlite3.Connection,
) -> None:
    """A model that does not read goalie form reports no gap and no credit for it."""
    _goalie_game(store)
    served = LogisticFit(("rest",), 0.0, 1.0, {"rest": 0.1})
    result = model(store, served).predict(GAME, AS_OF, 0.8)
    assert MissingKind.GOALIE_FORM not in {note.kind for note in result.missing}


def test_a_fit_with_the_goalie_feature_still_reports_the_goalie_form_gap(
    store: sqlite3.Connection,
) -> None:
    _goalie_game(store)
    served = LogisticFit(("goalie_gsax",), 0.0, 1.0, {"goalie_gsax": 0.1})
    result = model(store, served).predict(GAME, AS_OF, 0.8)
    kinds = {note.kind for note in result.missing}
    assert kinds & {MissingKind.GOALIE_FORM, MissingKind.GOALIE_UNKNOWN}


def test_the_starter_notes_stay_when_the_fit_has_no_goalie_feature(
    store: sqlite3.Connection,
) -> None:
    """A reader still learns that no starter is known, whatever the model reads."""
    season(store)
    served = LogisticFit(("rest",), 0.0, 1.0, {"rest": 0.1})
    result = model(store, served).predict(GAME, AS_OF, 0.8)
    assert MissingKind.GOALIE_UNKNOWN in {note.kind for note in result.missing}


def _training(
    store: sqlite3.Connection, through: int | None, start: str = "2026-01-20T00:00"
) -> TrainingInfo:
    """Return the training marker of one game, for a fit that trained through ``through``."""
    played = Game(100, "TOR", "BOS", utc(start))
    marker = (
        NhlGameModel(store, artifact(ELO_ONLY, through), GoalieMode.GUESS)
        .predict(played, utc("2026-01-19T16:00"), 0.8)
        .training
    )
    assert marker is not None
    return marker


def test_a_game_in_the_fit_seasons_is_in_sample(store: sqlite3.Connection) -> None:
    marker = _training(store, 20252026)
    assert marker == TrainingInfo(1, 20252026, TrainingMembership.IN_SAMPLE)


def test_a_game_in_a_later_season_is_out_of_sample(store: sqlite3.Connection) -> None:
    marker = _training(store, 20242025)
    assert marker.membership is TrainingMembership.OUT_OF_SAMPLE


def test_a_game_in_the_season_after_the_cutoff_is_out_of_sample(
    store: sqlite3.Connection,
) -> None:
    marker = _training(store, 20252026, start="2026-10-13T23:00")
    assert marker.membership is TrainingMembership.OUT_OF_SAMPLE


def test_a_burn_in_season_game_is_out_of_sample(store: sqlite3.Connection) -> None:
    """The fit reads no game of 2014-15, so the Elo burn-in season did not train it."""
    marker = _training(store, 20252026, start="2015-01-20T00:00")
    assert marker.membership is TrainingMembership.OUT_OF_SAMPLE


def test_a_fit_with_no_cutoff_says_the_membership_is_unknown(
    store: sqlite3.Connection,
) -> None:
    marker = _training(store, None)
    assert marker == TrainingInfo(1, None, TrainingMembership.UNKNOWN)


def test_the_as_of_time_does_not_clear_the_in_sample_marker(
    store: sqlite3.Connection,
) -> None:
    played = Game(100, "TOR", "BOS", utc("2026-01-20T00:00"))
    served = NhlGameModel(store, artifact(ELO_ONLY, 20252026), GoalieMode.GUESS)
    late = game_win_probability(played, served, as_of=utc("2026-01-21T00:00"))
    assert late.made_after_start
    assert late.training is not None
    assert late.training.membership is TrainingMembership.IN_SAMPLE


def test_the_eastern_game_day_decides_the_season_near_the_boundary(
    store: sqlite3.Connection,
) -> None:
    """A 02:00 UTC game on Sep 1 is Aug 31 in the East, so it is still season 2025-26."""
    marker = _training(store, 20252026, start="2026-09-01T02:00")
    assert marker.membership is TrainingMembership.IN_SAMPLE


def test_a_team_whose_moneypuck_form_lags_gets_a_note(store: sqlite3.Connection) -> None:
    # TOR has a MoneyPuck row only for game 15, and games 17 and 18 are final. MTL is current.
    for day in (15, 17, 18):
        add_game(store, day, f"2026-01-{day}T00:00", home="TOR", away="MTL")
        add_team_form(store, day, "MTL", 2.0, 3.0)
    add_team_form(store, 15, "TOR", 3.5, 2.0)
    played = Game(100, "TOR", "MTL", START)
    notes = [m.note for m in model(store).predict(played, AS_OF, 0.8).missing]
    assert [note for note in notes if "MoneyPuck form" in note] == [
        "MoneyPuck form is 2 stored games behind; newest MoneyPuck game 15, newest final game 18"
    ]
