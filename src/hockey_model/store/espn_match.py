"""Match an NHL game to its ESPN event (#35).

The match uses the team and start-time rule of the ESPN starter code. A game with two fitting
events is an error, because the match must not guess. Design: docs/design/pbp-shift-dataset.md
"""

from collections.abc import Sequence

from hockey_model.sources.espn_events import EspnEvent
from hockey_model.sources.nhl import GameRow
from hockey_model.store.db import StoreError
from hockey_model.store.reads import franchise_codes
from hockey_model.store.starter_writes import START_TOLERANCE


def match_event(game: GameRow, events: Sequence[EspnEvent]) -> EspnEvent | None:
    """Return the event of ``game``, or None when the day lists no such event.

    The event has the home and away teams of the game, as the same franchise, and starts within
    ``START_TOLERANCE`` of the game. A reversed pair is another game.
    """
    homes, aways = franchise_codes(game.home), franchise_codes(game.away)
    found = [
        event
        for event in events
        if event.home in homes
        and event.away in aways
        and abs(event.start - game.start) <= START_TOLERANCE
    ]
    if len(found) > 1:
        names = ", ".join(event.event_id for event in found)
        raise StoreError(
            f"game {game.game_id}: {len(found)} ESPN events fit ({names}); "
            "fix the stored schedule or the match"
        )
    return found[0] if found else None
