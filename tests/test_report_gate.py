"""Properties of the publish decision of one report trigger."""

import argparse
from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from hockey_model.report_gate import (
    REASON_ALREADY_PUBLISHED,
    REASON_DRY_RUN,
    REASON_DUE,
    REASON_LATE,
    REASON_NO_GAMES,
    REASON_REPLACE,
    GateDecision,
    decide,
    warning,
)
from hockey_model.store_cli import _report_decide

REASONS = (
    REASON_DRY_RUN,
    REASON_NO_GAMES,
    REASON_REPLACE,
    REASON_ALREADY_PUBLISHED,
    REASON_DUE,
    REASON_LATE,
)
_FACTS = {
    "due": st.booleans(),
    "force": st.booleans(),
    "dry_run": st.booleans(),
    "games": st.integers(min_value=0, max_value=20),
    "published": st.sampled_from([None, True, False]),
}


@given(**_FACTS)
def test_the_command_always_prints_the_same_five_keys_and_a_known_reason(
    due: bool, force: bool, dry_run: bool, games: int, published: bool | None
) -> None:
    args = argparse.Namespace(
        due=str(due).lower(),
        force=force,
        dry_run=dry_run,
        games=games,
        published="unknown" if published is None else str(published).lower(),
        date=date(2026, 11, 4),
        first_puck_drop="",
        margin_seconds=0,
    )
    payload, code = _report_decide(args)
    assert code == 0
    assert isinstance(payload, dict)
    assert set(payload) == {"due", "publish", "reason", "lookup_needed", "warning"}
    assert payload["reason"] in REASONS


@given(**_FACTS)
def test_a_dry_run_never_publishes_and_never_needs_the_release(
    due: bool, force: bool, dry_run: bool, games: int, published: bool | None
) -> None:
    decision = decide(due=due, force=force, dry_run=True, games=games, published=published)
    assert decision == GateDecision(due, False, REASON_DRY_RUN)
    assert not decision.lookup_needed


@given(**_FACTS)
def test_a_day_with_no_games_publishes_nothing(
    due: bool, force: bool, dry_run: bool, games: int, published: bool | None
) -> None:
    decision = decide(due=due, force=force, dry_run=False, games=0, published=published)
    assert (decision.due, decision.publish, decision.reason) == (False, False, REASON_NO_GAMES)
    assert not decision.lookup_needed


@given(**_FACTS)
def test_publish_implies_due_and_a_dry_run_is_never_published(
    due: bool, force: bool, dry_run: bool, games: int, published: bool | None
) -> None:
    decision = decide(due=due, force=force, dry_run=dry_run, games=games, published=published)
    assert not decision.publish or decision.due
    assert not (dry_run and decision.publish)


@given(due=st.booleans(), force=st.booleans(), games=st.integers(min_value=1, max_value=20))
def test_a_published_day_is_replaced_only_by_an_unforced_run_that_beats_the_puck_drop(
    due: bool, force: bool, games: int
) -> None:
    decision = decide(due=due, force=force, dry_run=False, games=games, published=True)
    if due and not force:
        assert decision.reason == REASON_REPLACE
        assert decision.publish
    else:
        assert decision.reason == REASON_ALREADY_PUBLISHED
        assert not decision.due
        assert not decision.publish


@given(due=st.booleans(), force=st.booleans(), games=st.integers(min_value=1, max_value=20))
def test_a_day_with_no_release_always_publishes_and_a_late_run_says_so(
    due: bool, force: bool, games: int
) -> None:
    decision = decide(due=due, force=force, dry_run=False, games=games, published=False)
    assert decision.publish
    assert (decision.reason == REASON_LATE) == (not due)


@given(**_FACTS)
def test_the_release_is_asked_for_exactly_when_it_decides_the_answer(
    due: bool, force: bool, dry_run: bool, games: int, published: bool | None
) -> None:
    decision = decide(due=due, force=force, dry_run=dry_run, games=games, published=published)
    expected = published is None and not dry_run and games > 0
    assert decision.lookup_needed == expected


@given(
    reason=st.sampled_from(REASONS),
    day=st.dates().map(str),
    first_drop=st.sampled_from(["", "2026-11-04T23:00:00Z"]),
    margin=st.integers(min_value=0, max_value=3600),
)
def test_only_a_late_run_and_an_empty_day_warn(
    reason: str, day: str, first_drop: str, margin: int
) -> None:
    note = warning(
        GateDecision(True, True, reason), day=day, first_drop=first_drop, margin_seconds=margin
    )
    assert (note is not None) == (reason in {REASON_LATE, REASON_NO_GAMES})
    if note is not None:
        assert note.startswith("::warning::") and day in note
    if reason == REASON_LATE:
        assert note is not None
        assert str(margin) in note
        assert first_drop in note
