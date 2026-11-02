# hockey-model API UX design

## Intent

Turn internal modeling code from several projects into one library.
Two consumers use it first: leaguecheatr and sportsbettr.
Other tools follow later.
This document covers what callers see: the entry points, the objects, and the errors.
It does not cover where the data comes from or how the models work.

## Goals

- One Python API that every consumer imports.
- Fantasy and live-roster callers use the same calls.
- Every projection states its uncertainty.
- Ambiguous input fails loudly.

## Non-goals

- A network service. The library runs in the caller's process.
- Choosing data sources or models. The reference repos supply them, and the builder decides how.
- DataFrame output in v1.

## Anti-goals

- A name that matches two players must not pick one silently.
- A partial result must not look like a complete one.

## Surfaces

- The Python import is the primary surface.
- A CLI wraps the same calls and prints the same objects as JSON, for scripts.
- The CLI adds no logic of its own.

## Player identity

A caller identifies a player in one of three ways:

- Our canonical ID. It resolves exactly.
- A source ID, such as a Yahoo ID or an NHL ID. It resolves exactly.
- A name. The name must match exactly one player.

Zero matches raise an error.
Several matches raise an error that lists each candidate with enough detail to pick one.
The caller then retries with an ID.

**My call:** name matching ignores case and accents.
It does not match nicknames.

**My call:** name search covers every player the library has data for, active or retired.
A name that matches an active and a retired player is an ambiguity error, the same as two active players.

## Raw queries

Three query families return typed objects: players, teams, and standings.
`Player`, `Team`, and `Standing` have fixed fields, so an IDE can complete them.

## Rosters

A roster is a collection of players with optional slot assignments.
It is the one object every team-level call takes, whether the roster came from a fantasy league or from an NHL club.

**My call:** the type is named `Roster`.
The earlier draft called it `FantasyTeam`, but NHL clubs use the same type, so a fantasy-only name would mislead.

Meta-APIs build rosters:

- `nhl_roster(team_code, as_of=date)` returns the NHL club's roster at a point in time.
- Fantasy rosters come from the fantasy source. The caller passes the player list and slots.

A live NHL roster and a fantasy roster follow the same path after construction.

## Slots and eligibility

- Callers pass slot assignments on each roster. The library reads no league config.
- Slot names: C, W, D, G, UTIL, bench. Injured reserve is not modeled in v1.
- A player may be eligible for more than one position (for example C and W), and the fit check accepts any eligible slot.
- An empty slot raises an error. Zero is never assumed.
- A slot whose position does not fit the player raises an error. For example, a goalie cannot fill a forward slot.
- Without slots, every player counts.

## Stat projections

One call projects one stat for one player, over the next game by default.
The result holds a point estimate and an interval.

**My call:** the interval is 80%.
A caller that needs another level gets a parameter, and the default stays 80%.

**My call:** the horizon is a parameter with a default of the next game.
A caller can request more games.

## Team projections

A team projection takes a roster and returns, for each stat:

- The total over the roster's counting players.
- An 80% interval on that total.
- The per-player breakdown.

**Decided:** the team interval comes from a joint model, so the 80% is true on the total.
Players are not treated as independent.
The sum of per-player intervals is not used, because it is not a true 80% interval.

## Rate stats

Rate stats (save percentage, goals against average, shooting percentage) are never summed.
A player-level rate stat returns its own value and interval.

**My call:** a team-level call for a rate stat raises an error in v1.
A weighted average needs a weighting rule that nobody has specified yet.

## Errors

Every error names the operation, the input, and the fix.
A partial result is an error, not a quiet return.
The caller never receives a total that left out players it passed in.

**My call:** a player in a starting slot with no projection raises an error.
A player on the bench with no projection does not raise an error, because bench players never count.
A caller can see such a player in the roster it passed in, so nothing is hidden.

## Matchups

A matchup takes two rosters, home and away, and returns:

- The win probability for each side.
- The expected goals for each side, with an interval.

The two rosters can be any mix of NHL and fantasy rosters.
A matchup does not return player projections.
A caller that needs them calls the player and roster functions.

**Decided:** the matchup input is two rosters, built from NHL or fantasy data.

**My call:** the win probability counts overtime and shootout wins as wins for the side that takes them, so the two sides sum to one.
The overtime split is the builder's to model.

## Playoff likelihood

A caller can ask for the probability that a team makes the playoffs.

**My call:** it is a separate function, not part of the matchup.
Its horizon is the rest of the season.
It takes a team code, not a roster, because playoff odds depend on the standings.

## Player fields

Each object exposes these fields. Names are the contract; types follow the builder.

- `Player`: canonical ID, source IDs (Yahoo, NHL), full name, position eligibility (one or more of C, W, D, G), current team.
- `Team`: NHL team code, name, division, conference.
- `Standing`: team, games played, points, regulation-plus-overtime wins, rank in its league.
- `Roster`: a named collection of players with optional slot assignments.

## Open for the builder

These questions do not change the UX above.

- Which data source feeds each query family.
- The model behind each projection, and how it produces the interval.
- How the joint team interval is modeled.
- Storage, caching, and refresh.
- The exact stat names and units.
- How a live roster reads from the source.
- How playoff odds are modeled.

## Calls made here

- Name matching ignores case and accents, and does not match nicknames.
- Name search covers active and retired players. An ambiguous name raises an error.
- The default interval is 80%. The default horizon is the next game.
- The team interval comes from a joint model, not a sum of per-player intervals.
- The type is `Roster`, shared by fantasy and NHL club rosters.
- A slot that does not fit a player's position raises an error.
- A starter with no projection raises an error. A bench player with no projection does not.
- Rate stats are never summed. A team-level rate stat raises an error in v1.
- Matchups take two rosters and return win probability and expected goals with an interval.
- Overtime and shootout wins count for the side that takes them.
- Playoff odds are a separate function over the rest of the season.
- Injured reserve is not modeled in v1.
