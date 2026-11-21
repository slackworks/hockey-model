"""The plain-language notes that a backtest report carries."""

from collections.abc import Sequence

from hockey_model.backtest.eligibility import SeasonCount
from hockey_model.game_model.features import FEATURES, GOALIE_FEATURE

MONEYPUCK_LEAK_NOTE = (
    "The MoneyPuck expected-goals model is today's model. MoneyPuck possibly trained it on "
    "games after the scored seasons, so results that use MoneyPuck data may look better than "
    "they would have then."
)
GUESS_EQUALS_ACTUAL_NOTE = (
    "No tested feature reads the starting goalie, so the actual run and the guess run are the "
    "same by construction. The equal scores do not show that a wrong goalie costs nothing."
)
INJURY_HISTORY_NOTE = (
    "The store has no ESPN injury history before it began to keep injury reads, so the goalie "
    "guess run never leaves out an injured goalie. The live model does."
)
# Every feature here reads MoneyPuck data, so a run that uses one carries the credit and the note.
MONEYPUCK_FEATURES = frozenset({"xg_form", "attempt_form", "goalie_gsax"})


def joint_test_notes(
    tested: Sequence[str], vetoed: Sequence[str], success: bool
) -> tuple[str, ...]:
    count = len(tested) + len(vetoed)
    notes = [f"{count} candidate features were tested together as one joint test."]
    if vetoed:
        notes.append(f"The guess run vetoed: {', '.join(vetoed)}.")
    if not success:
        notes.append("The joint test failed, so the Elo alone ships.")
    if MONEYPUCK_FEATURES & set(tested):
        notes.append(MONEYPUCK_LEAK_NOTE)
    if GOALIE_FEATURE not in tested:
        notes.append(GUESS_EQUALS_ACTUAL_NOTE)
    notes.append(INJURY_HISTORY_NOTE)
    return tuple(notes)


def is_production(candidates: Sequence[str]) -> bool:
    return tuple(candidates) == FEATURES


def candidate_notes(candidates: Sequence[str]) -> tuple[str, ...]:
    """Say so when a run tests a set other than the production set, so no result reads as live."""
    if is_production(candidates):
        return ()
    return (
        "This candidate set is a test. Nothing ships from it, and the live model reads only the "
        "production features.",
    )


def eligibility_notes(reasons: Sequence[str], coverage: Sequence[SeasonCount]) -> tuple[str, ...]:
    """Say why success is refused, and name the seasons with stored games that never finished."""
    notes = [f"Not eligible for success: {reason}." for reason in reasons]
    notes.extend(
        f"Season {c.season} game type {c.game_type} holds {c.unresolved} stored game(s) that are "
        "not final long after their start."
        for c in coverage
        if c.unresolved
    )
    return tuple(notes)
