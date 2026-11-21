"""Tests for the game features."""

import math
import sqlite3
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from seeding import (
    add_game,
    add_goalie,
    add_goalie_form,
    add_starter_read,
    add_team_form,
    utc,
)

from hockey_model.game import Game, GoalieStart, MissingKind, StarterSource, StarterStatus
from hockey_model.game_model.elo import EloBook
from hockey_model.game_model.features import (
    ATTEMPT_FEATURES,
    FEATURES,
    NEUTRAL_FORM,
    REST_CAP_DAYS,
    GoalieMode,
    build_features,
)
from hockey_model.store.db import connect
from hockey_model.types import Player, Slot

START = utc("2026-01-20T00:00")
AS_OF = utc("2026-01-19T16:00")
HOME, AWAY = "TOR", "BOS"


def game() -> Game:
    return Game(100, HOME, AWAY, START)


def features(
    store: sqlite3.Connection,
    mode: GoalieMode = GoalieMode.GUESS,
    as_of=AS_OF,
    names: tuple[str, ...] = FEATURES,
):
    return build_features(store, game(), as_of, EloBook(), goalie_mode=mode, names=names)


def rest_only(conn: sqlite3.Connection, game: Game):
    """Build the rest feature of ``game`` alone."""
    return build_features(
        conn, game, AS_OF, EloBook(), goalie_mode=GoalieMode.GUESS, names=("rest",)
    )


def kinds(row) -> list[MissingKind]:
    return [m.kind for m in row.missing]


def history(store: sqlite3.Connection, days: tuple[int, ...] = (17, 18, 19)) -> None:
    """Games of TOR and BOS on the given January days, each with MoneyPuck form."""
    for day in days:
        gid = 10 + day
        add_game(store, gid, f"2026-01-{day:02d}T00:00", home=HOME, away="MTL")
        add_game(store, gid + 100, f"2026-01-{day:02d}T00:00", home="MTL", away=AWAY)
        add_team_form(store, gid, HOME, 3.0, 1.0)
        add_team_form(store, gid + 100, AWAY, 1.0, 3.0)
        add_goalie(store, gid, HOME, 11, "C. Ingram")
        add_goalie(store, gid + 100, AWAY, 21, "J. Swayman")
        add_goalie_form(store, gid, 11, 3.0, 1)
        add_goalie_form(store, gid + 100, 21, 2.0, 3)


def test_feature_names() -> None:
    assert FEATURES == ("xg_form", "goalie_gsax", "rest")


def test_elo_logit_matches_the_book(store: sqlite3.Connection) -> None:
    chance = EloBook().home_win_chance(HOME, AWAY)
    row = features(store)
    assert row.elo_logit == pytest.approx(math.log(chance / (1 - chance)))
    assert row.game_id == 100
    assert set(row.values) == set(FEATURES)


def test_xg_form_is_home_share_minus_away_share(store: sqlite3.Connection) -> None:
    history(store)
    row = features(store)
    assert row.values["xg_form"] == pytest.approx(0.75 - 0.25)
    assert row.uses_moneypuck


def test_xg_form_uses_the_window(store: sqlite3.Connection) -> None:
    base = utc("2025-12-01T00:00")
    for number in range(13):
        old = number < 3
        gid = 200 + number
        stamp = (base + timedelta(days=number)).strftime("%Y-%m-%dT%H:%M")
        add_game(store, gid, stamp, home=HOME, away="MTL")
        add_team_form(store, gid, HOME, 1.0 if old else 3.0, 3.0 if old else 1.0)
        add_game(store, gid + 100, stamp, home="MTL", away=AWAY)
        add_team_form(store, gid + 100, AWAY, 2.0, 2.0)
    row = features(store)
    assert row.values["xg_form"] == pytest.approx(0.75 - 0.5)


def test_xg_form_missing_reports_xg_form(store: sqlite3.Connection) -> None:
    row = features(store)
    assert row.values["xg_form"] == 0.0
    assert kinds(row).count(MissingKind.XG_FORM) == 2
    assert not row.uses_moneypuck


def test_xg_form_missing_for_one_team(store: sqlite3.Connection) -> None:
    history(store)
    add_game(store, 300, "2026-01-18T00:00", home="NYR", away="MTL")
    row = build_features(
        store,
        Game(101, "NYR", AWAY, START),
        AS_OF,
        EloBook(),
        goalie_mode=GoalieMode.GUESS,
        names=FEATURES,
    )
    assert row.values["xg_form"] == 0.0
    assert [m.team for m in row.missing if m.kind is MissingKind.XG_FORM] == ["NYR"]


def test_goalie_gsax_actual_uses_the_boxscore_starter(store: sqlite3.Connection) -> None:
    history(store)
    add_game(store, 100, "2026-01-20T00:00")
    add_goalie(store, 100, HOME, 12, "J. Woll")
    add_goalie(store, 100, AWAY, 21, "J. Swayman")
    add_goalie_form(store, 50, 12, 1.0, 3)
    add_game(store, 50, "2026-01-15T00:00", home=HOME, away="MTL")
    row = features(store, GoalieMode.ACTUAL, as_of=START)
    woll = 1.0 - 3
    swayman = 2.0 - 3
    assert row.values["goalie_gsax"] == pytest.approx(woll - swayman)


def test_goalie_gsax_guess_uses_recent_starts(store: sqlite3.Connection) -> None:
    history(store)
    row = features(store, GoalieMode.GUESS)
    assert row.values["goalie_gsax"] == pytest.approx((3.0 - 1) - (2.0 - 3))
    assert MissingKind.GOALIE_UNKNOWN in kinds(row)


def test_goalie_gsax_reads_use_a_confirmed_goalie(store: sqlite3.Connection) -> None:
    history(store)
    add_game(store, 50, "2026-01-15T00:00", home=HOME, away="MTL")
    add_goalie(store, 50, HOME, 12, "J. Woll")
    add_goalie_form(store, 50, 12, 0.5, 4)
    add_starter_read(
        store, 100, HOME, "espn", "Joseph Woll", "confirmed", fetched_at="2026-01-19T14:00"
    )
    row = features(store, GoalieMode.READS)
    assert row.values["goalie_gsax"] == pytest.approx((0.5 - 4) - (2.0 - 3))


def test_goalie_gsax_missing_form_reports_goalie_form(store: sqlite3.Connection) -> None:
    for day in (17, 18):
        add_game(store, day, f"2026-01-{day}T00:00")
        add_goalie(store, day, HOME, 11, "C. Ingram")
    row = features(store)
    assert row.values["goalie_gsax"] == 0.0
    assert MissingKind.GOALIE_FORM in kinds(row)


def test_goalie_gsax_actual_without_boxscore_reports_unknown(store: sqlite3.Connection) -> None:
    history(store)
    row = features(store, GoalieMode.ACTUAL)
    assert row.values["goalie_gsax"] == 0.0
    assert kinds(row).count(MissingKind.GOALIE_UNKNOWN) == 2


def test_goalie_form_uses_only_the_window(store: sqlite3.Connection) -> None:
    base = utc("2025-12-01T00:00")
    for number in range(23):
        gid = 400 + number
        stamp = (base + timedelta(days=number)).strftime("%Y-%m-%dT%H:%M")
        add_game(store, gid, stamp, home=HOME, away="MTL")
        add_goalie(store, gid, HOME, 11, "C. Ingram")
        add_goalie_form(store, gid, 11, 5.0 if number < 3 else 1.0, 0)
        # The away goalie has a neutral form, so the differential is the home window alone.
        add_goalie(store, gid, AWAY, 21, "M. Away")
        add_goalie_form(store, gid, 21, 0.0, 0)
    row = features(store, GoalieMode.GUESS)
    assert row.values["goalie_gsax"] == pytest.approx(1.0)


def test_rest_back_to_back_is_one(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-19T00:00", home=HOME, away="MTL")
    add_game(store, 2, "2026-01-18T00:00", home="NYR", away=AWAY)
    add_game(store, 3, "2026-01-16T00:00", home="NYR", away=AWAY)
    row = features(store)
    assert row.values["rest"] == pytest.approx(1.0 - 2.0)


def test_rest_caps_at_four(store: sqlite3.Connection) -> None:
    add_game(store, 1, "2026-01-01T00:00", home=HOME, away="MTL")
    add_game(store, 2, "2026-01-12T00:00", home="NYR", away=AWAY)
    row = features(store)
    assert row.values["rest"] == 0.0
    assert REST_CAP_DAYS == 4.0


def test_rest_with_the_away_team_new_to_the_store_is_neutral(store: sqlite3.Connection) -> None:
    add_game(store, 2, "2026-01-18T00:00", home=HOME, away="MTL")
    add_game(store, 3, "2026-01-20T00:00", home=HOME, away=AWAY)
    row = features(store)
    assert row.values["rest"] == NEUTRAL_FORM
    notes = [m for m in row.missing if m.kind is MissingKind.REST_UNKNOWN]
    assert [m.team for m in notes] == [AWAY]
    assert notes[0].note == "no earlier game in the store; the rest difference is neutral"


def test_rest_with_the_home_team_new_to_the_store_is_neutral(store: sqlite3.Connection) -> None:
    add_game(store, 2, "2026-01-18T00:00", home="NYR", away=AWAY)
    row = features(store)
    assert row.values["rest"] == NEUTRAL_FORM
    assert [m.team for m in row.missing if m.kind is MissingKind.REST_UNKNOWN] == [HOME]


def test_rest_with_both_teams_new_to_the_store_is_neutral(store: sqlite3.Connection) -> None:
    row = features(store)
    assert row.values["rest"] == NEUTRAL_FORM
    assert sorted(m.team for m in row.missing if m.kind is MissingKind.REST_UNKNOWN) == [AWAY, HOME]


def test_a_relocated_franchise_has_its_rest_from_the_old_code(store: sqlite3.Connection) -> None:
    # Arizona's games count for Utah, so Utah's first game of a season has a rest.
    add_game(store, 1, "2026-01-18T00:00", home="ARI", away="MTL")
    add_game(store, 2, "2026-01-18T00:00", home="NYR", away=AWAY)
    row = rest_only(store, Game(100, "UTA", AWAY, START))
    assert MissingKind.REST_UNKNOWN not in kinds(row)
    assert row.values["rest"] == pytest.approx(0.0)


@settings(max_examples=40, deadline=None)
@given(
    home_gap=st.one_of(st.none(), st.integers(1, 8)),
    away_gap=st.one_of(st.none(), st.integers(1, 8)),
)
def test_swapping_the_teams_negates_a_known_rest_difference_and_keeps_a_neutral_one(
    tmp_path_factory: pytest.TempPathFactory, home_gap: int | None, away_gap: int | None
) -> None:
    conn = connect(tmp_path_factory.mktemp("store") / "store.db")
    try:
        for number, (team, gap, other) in enumerate(
            ((HOME, home_gap, "MTL"), (AWAY, away_gap, "NYR")), start=1
        ):
            if gap is not None:
                day = (START - timedelta(days=gap)).strftime("%Y-%m-%dT%H:%M")
                add_game(conn, number, day, home=team, away=other)
        straight = rest_only(conn, Game(100, HOME, AWAY, START))
        swapped = rest_only(conn, Game(100, AWAY, HOME, START))
    finally:
        conn.close()
    assert swapped.values["rest"] == -straight.values["rest"]
    if home_gap is None or away_gap is None:
        assert straight.values["rest"] == NEUTRAL_FORM
    else:
        expected = min(home_gap, REST_CAP_DAYS) - min(away_gap, REST_CAP_DAYS)
        assert straight.values["rest"] == pytest.approx(expected)


def test_features_use_only_games_before_start(store: sqlite3.Connection) -> None:
    history(store)
    before = features(store, GoalieMode.GUESS)
    for gid, day in ((900, 21), (901, 22)):
        add_game(store, gid, f"2026-01-{day}T00:00", home=HOME, away=AWAY)
        add_team_form(store, gid, HOME, 0.1, 5.0)
        add_team_form(store, gid, AWAY, 5.0, 0.1)
        add_goalie(store, gid, HOME, 12, "J. Woll")
        add_goalie_form(store, gid, 12, 0.0, 9)
    after = features(store, GoalieMode.GUESS)
    assert after == before


def test_a_late_run_ignores_the_game_itself(store: sqlite3.Connection) -> None:
    history(store)
    add_game(store, 100, "2026-01-20T00:00")
    add_team_form(store, 100, HOME, 0.1, 5.0)
    add_team_form(store, 100, AWAY, 5.0, 0.1)
    for team, nhl_id, name in ((HOME, 11, "C. Ingram"), (AWAY, 21, "J. Swayman")):
        add_goalie(store, 100, team, nhl_id, name)
        add_goalie_form(store, 100, nhl_id, 0.0, 9)
    late = START + timedelta(hours=6)
    on_time = features(store, GoalieMode.GUESS, as_of=AS_OF)
    after = features(store, GoalieMode.GUESS, as_of=late)
    assert after.values == on_time.values


def test_guess_mode_ignores_the_boxscore_even_after_puck_drop(store: sqlite3.Connection) -> None:
    history(store)
    add_game(store, 100, "2026-01-20T00:00")
    add_goalie(store, 100, HOME, 12, "J. Woll")
    add_goalie(store, 100, AWAY, 21, "J. Swayman")
    add_goalie_form(store, 50, 12, 0.5, 4)
    add_game(store, 50, "2026-01-15T00:00", home=HOME, away="MTL")
    guess = features(store, GoalieMode.GUESS, as_of=START)
    actual = features(store, GoalieMode.ACTUAL, as_of=START)
    assert MissingKind.LATE_RUN not in kinds(guess)
    assert guess.values["goalie_gsax"] == pytest.approx((3.0 - 1) - (2.0 - 3))
    assert actual.values["goalie_gsax"] != guess.values["goalie_gsax"]


def test_a_team_with_no_earlier_game_reports_rest_unknown(store: sqlite3.Connection) -> None:
    row = features(store)
    assert kinds(row).count(MissingKind.REST_UNKNOWN) == 2
    assert row.values["rest"] == 0.0


def test_a_team_with_an_earlier_game_does_not_report_rest_unknown(
    store: sqlite3.Connection,
) -> None:
    add_game(store, 1, "2026-01-18T00:00", home=HOME, away="MTL")
    add_game(store, 2, "2026-01-17T00:00", home="NYR", away=AWAY)
    assert MissingKind.REST_UNKNOWN not in kinds(features(store))


def test_a_goalie_with_no_nhl_id_reports_goalie_form(store: sqlite3.Connection) -> None:
    player = Player("x", None, None, "No Id", frozenset({Slot.G}), HOME)
    given = GoalieStart(
        player, StarterStatus.CONFIRMED, StarterSource.ESPN, AS_OF - timedelta(hours=1)
    )
    row = build_features(
        store,
        Game(100, HOME, AWAY, START, home_goalie=given),
        AS_OF,
        EloBook(),
        goalie_mode=GoalieMode.READS,
        names=FEATURES,
    )
    assert any(
        m.kind is MissingKind.GOALIE_FORM and "no NHL id" in m.note and m.team == HOME
        for m in row.missing
    )


def test_attempt_form_uses_shot_attempts_not_expected_goals(store: sqlite3.Connection) -> None:
    # Expected goals give a 0.75 and 0.25 share. The attempt counts give 0.6 and 0.3.
    add_game(store, 10, "2026-01-18T00:00", home=HOME, away="MTL")
    add_game(store, 110, "2026-01-18T00:00", home="MTL", away=AWAY)
    add_team_form(store, 10, HOME, 3.0, 1.0, attempts=(60, 40))
    add_team_form(store, 110, AWAY, 1.0, 3.0, attempts=(30, 70))
    row = features(store, names=("xg_form", "attempt_form", "goalie_gsax", "rest"))
    assert row.values["attempt_form"] == pytest.approx(0.6 - 0.3)
    assert row.values["xg_form"] == pytest.approx(0.75 - 0.25)
    assert row.uses_moneypuck


def test_attempt_form_missing_reports_attempt_form(store: sqlite3.Connection) -> None:
    row = features(store, names=ATTEMPT_FEATURES)
    assert row.values["attempt_form"] == 0.0
    assert kinds(row).count(MissingKind.ATTEMPT_FORM) == 2


def test_production_features_do_not_read_attempts(store: sqlite3.Connection) -> None:
    row = features(store)
    assert "attempt_form" not in row.values
    assert MissingKind.ATTEMPT_FORM not in kinds(row)


def test_attempt_candidates_do_not_read_expected_goals(store: sqlite3.Connection) -> None:
    row = features(store, names=ATTEMPT_FEATURES)
    assert "xg_form" not in row.values
    assert MissingKind.XG_FORM not in kinds(row)


def test_an_unknown_feature_name_is_refused(store: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown feature 'nope'"):
        features(store, names=("nope",))


def test_a_zero_total_game_is_left_out_of_the_share_and_named(store: sqlite3.Connection) -> None:
    history(store)
    add_game(store, 400, "2026-01-16T00:00", home=HOME, away="MTL")
    add_team_form(store, 400, HOME, 0.0, 0.0)
    row = features(store)
    assert row.values["xg_form"] == pytest.approx(0.75 - 0.25)
    notes = [m for m in row.missing if m.kind is MissingKind.FORM_GAMES_LEFT_OUT]
    assert [(m.team, m.note) for m in notes] == [
        (HOME, "1 of 4 MoneyPuck games have a zero total and are left out")
    ]
    assert MissingKind.XG_FORM not in kinds(row)
    assert row.uses_moneypuck
    assert "xg_form" not in [d.feature for d in row.imputed]


def test_a_team_whose_every_game_has_a_zero_total_makes_the_form_neutral(
    store: sqlite3.Connection,
) -> None:
    add_game(store, 400, "2026-01-16T00:00", home=HOME, away="MTL")
    add_team_form(store, 400, HOME, 0.0, 0.0)
    add_game(store, 401, "2026-01-16T00:00", home="MTL", away=AWAY)
    add_team_form(store, 401, AWAY, 3.0, 1.0)
    row = features(store)
    assert row.values["xg_form"] == NEUTRAL_FORM
    notes = [(m.team, m.note) for m in row.missing if m.kind is MissingKind.XG_FORM]
    assert notes == [(HOME, "every one of 1 MoneyPuck games before this game has a zero total")]
    degraded = {d.feature: d.teams for d in row.imputed}
    assert degraded["xg_form"] == (HOME,)


def test_a_measured_zero_share_is_valid_and_has_no_note(store: sqlite3.Connection) -> None:
    add_game(store, 400, "2026-01-16T00:00", home=HOME, away="MTL")
    add_team_form(store, 400, HOME, 0.0, 2.0)
    add_game(store, 401, "2026-01-16T00:00", home="MTL", away=AWAY)
    add_team_form(store, 401, AWAY, 1.0, 3.0)
    row = features(store)
    assert row.values["xg_form"] == pytest.approx(0.0 - 0.25)
    assert [m for m in row.missing if m.kind is MissingKind.XG_FORM] == []


def test_a_set_without_rest_reports_no_rest_gap(store: sqlite3.Connection) -> None:
    row = features(store, names=("xg_form",))
    assert MissingKind.REST_UNKNOWN not in kinds(row)
    assert "rest" not in [d.feature for d in row.imputed]


def test_a_set_with_rest_reports_the_rest_gap_of_each_new_team(store: sqlite3.Connection) -> None:
    row = features(store, names=("rest",))
    assert kinds(row).count(MissingKind.REST_UNKNOWN) == 2
