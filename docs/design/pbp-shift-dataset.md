# Play-by-play and shift dataset (#35)

## Goal

Store NHL play-by-play events, shift charts, and a sample of ESPN game summaries, with every revision.
Derive stints (spans with one set of players on the ice) and check them against independent totals.
This issue builds data only. No model reads it, and no prediction changes.

The owner decisions in the issue body are settled: the three endpoints are approved, the revisit days, the ESPN sample, and the reconciliation checks.
This design adds the exact tables and the stint method.

## Prerequisites

- [payload-archive.md](payload-archive.md) (#82): the `payloads` and `observations` tables and `store rebuild`. The issue body planned its own `raw_payloads` table; use the shared archive instead, so one scheme serves both issues.

## Sources

| Source name | URL | Host policy |
| --- | --- | --- |
| `nhl_pbp` | `https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play` | existing `api-web.nhle.com` policy |
| `nhl_shifts` | `https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}` | add `api.nhle.com` to `HOST_POLICIES` in `sources/policy.py` with `min_interval_s=0.5` |
| `espn_summary` | `https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event={espn_id}` | existing ESPN policy; sanitize with `strip_market_fields` before the hash |

Do not add these names to `KNOWN_SOURCES` in `store/source_reads.py` (issue body rule).

## Tables

Schema version 5, through `hockey-model store upgrade`.
Every derived row names the observation it came from, so a rebuild can replace one derivation.

```sql
CREATE TABLE pbp_events (
    obs_id INTEGER NOT NULL REFERENCES observations (obs_id),
    game_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    sort_order INTEGER NOT NULL,
    period INTEGER NOT NULL,
    period_type TEXT NOT NULL CHECK (period_type IN ('REG', 'OT', 'SO')),
    period_second INTEGER NOT NULL,   -- seconds since the period start, from timeInPeriod
    game_second INTEGER,              -- NULL in a shootout
    type TEXT NOT NULL,               -- typeDescKey
    situation_code TEXT,              -- four digits: away goalie, away skaters, home skaters, home goalie
    owner_team TEXT,                  -- team code of eventOwnerTeamId
    x REAL, y REAL,                   -- raw feed coordinates; NULL when absent
    attack_x_sign INTEGER,            -- +1 or -1: the sign of x toward the net that owner_team attacks; NULL when unknown
    zone TEXT, shot_type TEXT,
    player1 INTEGER, player2 INTEGER, player3 INTEGER,   -- see the role map below
    goalie_in_net INTEGER,
    home_score INTEGER, away_score INTEGER,              -- after the event, when the feed gives it
    PRIMARY KEY (obs_id, event_id)
);
CREATE TABLE shifts (
    obs_id INTEGER NOT NULL REFERENCES observations (obs_id),
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    team TEXT NOT NULL,
    period INTEGER NOT NULL,
    start_second INTEGER NOT NULL,    -- game seconds
    end_second INTEGER NOT NULL,
    shift_number INTEGER,
    PRIMARY KEY (obs_id, player_id, period, start_second)
);
CREATE TABLE stints (
    game_id INTEGER NOT NULL,
    pbp_obs INTEGER NOT NULL, shift_obs INTEGER NOT NULL,
    stint INTEGER NOT NULL,           -- 1, 2, ... in time order
    period INTEGER NOT NULL,
    start_second INTEGER NOT NULL, end_second INTEGER NOT NULL,
    home_skaters TEXT NOT NULL, away_skaters TEXT NOT NULL,   -- JSON arrays of sorted ids
    home_goalie INTEGER, away_goalie INTEGER,                 -- NULL when the net is empty
    PRIMARY KEY (game_id, pbp_obs, shift_obs, stint)
);
CREATE TABLE espn_skater_toi (
    obs_id INTEGER NOT NULL, game_id INTEGER NOT NULL, nhl_id INTEGER,
    espn_name TEXT NOT NULL, toi INTEGER, pp_toi INTEGER, sh_toi INTEGER, es_toi INTEGER,
    shifts INTEGER,
    PRIMARY KEY (obs_id, espn_name)
);
CREATE TABLE event_status (
    game_id INTEGER NOT NULL, pbp_obs INTEGER NOT NULL, shift_obs INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('provisional', 'reconciled', 'incomplete')),
    checked_at TEXT NOT NULL,
    notes TEXT NOT NULL,              -- JSON list of failed check names and values
    PRIMARY KEY (game_id, pbp_obs, shift_obs)
);
```

Add views `latest_pbp_events`, `latest_shifts`, and `latest_stints` that pick the rows of the newest observation of each game.

Role map for `player1` to `player3` (verify on the fixtures, and change the map if a feed key differs):

| type | player1 | player2 | player3 |
| --- | --- | --- | --- |
| `goal` | `scoringPlayerId` | `assist1PlayerId` | `assist2PlayerId` |
| `shot-on-goal`, `missed-shot` | `shootingPlayerId` | | |
| `blocked-shot` | `shootingPlayerId` | `blockingPlayerId` | |
| `faceoff` | `winningPlayerId` | `losingPlayerId` | |
| `penalty` | `committedByPlayerId` | `drawnByPlayerId` | `servedByPlayerId` |
| `hit` | `hittingPlayerId` | `hitteePlayerId` | |
| `giveaway`, `takeaway` | `playerId` | | |

## Time and direction

- `game_second = (period - 1) * 1200 + period_second` for periods 1 to 3. A regular season OT period starts at 3600. A playoff OT period `p` starts at `3600 + (p - 4) * 1200`.
- A shootout event (`period_type = 'SO'`) gets `game_second` NULL and is in no stint.
- Direction: `homeTeamDefendingSide` is `left` or `right` for the event. The home team attacks the other side. `attack_x_sign` is `+1` when the owner team attacks the net at positive x. When the feed has no defending side, `attack_x_sign` is NULL. Never guess it from the period number.

## Stint method

For each game and period:

1. Shift clocks are `MM:SS` from the period start. Convert to game seconds. Compute the length from the start and the end, because `duration` can be null.
2. Drop a shift with `end <= start` and add its row to the status notes.
3. Merge the shifts of one player that overlap or touch into one interval.
4. The boundaries are every merged start and end. Between two neighbor boundaries `a < b`, the players on the ice are the merged intervals with `start <= a` and `end >= b`.
5. Split the players by team and by position (`G` from `rosterSpots` of the play-by-play). A team with no goalie on the ice has an empty net.
6. Join neighbor spans that have the same players into one stint.
7. An event at game second `t` belongs to the stint with `start < t <= end`. A goal at a line change therefore belongs to the players on the ice before the change. An event at `t = 0` of a period belongs to the first stint of that period.

## Reconciliation

Each check writes its result into `event_status.notes`.

1. Goals by team in the play-by-play, without shootout goals, equal the stored final score minus the shootout winner increment.
2. Goals by player equal `skater_games.goals`.
3. Each skater's merged shift time is within `TOI_TOLERANCE_S` of `skater_games.toi_seconds`. Measure the differences on the first 500 backfilled games, set the constant at the 99th percentile, rounded up to 5 seconds, and freeze it in that pull request.
4. In the ESPN sample, stint time by strength is within `STRENGTH_TOI_TOLERANCE_S` of `PPTOI`, `SHTOI`, and `ESTOI`, and the merged shift count equals `SHFT`. Set the tolerance in the same way.
5. A stint with fewer skaters than `situation_code` gives, at an event inside it, marks the game `incomplete`. No player is added.

Status: `provisional` until the last revisit (`REVISIT_AFTER_DAYS` of the archive design). Then `reconciled` when every check passed, else `incomplete`.

## Refresh and backfill

- One refresh unit for each final game and source, with the boxscore rules: one transaction, stop after `MAX_UNIT_FAILURES`, abandon after the existing rule.
- Revisit at the archive revisit days.
- Backfill: every stored final game from the 2014-15 season, newest season first, inside `--budget-seconds`.
- ESPN: each new final game, plus a sample of 150 regular season games of each season chosen with `random.Random(season).sample(sorted(game_ids), 150)`.
- ESPN to NHL game match: reuse the team and start-time match of the ESPN starter code.

## Tests

The list in the issue body is the acceptance list. Add these:

- `hypothesis` property test: for generated shift lists of 2 teams, the stints cover each player's merged time exactly once, and the sum of stint lengths equals the period length covered.
- An OT period of a regular season game and of a playoff game get the right `game_second`.
- An event with no `homeTeamDefendingSide` gets `attack_x_sign` NULL.
- `store rebuild --source nhl_pbp` gives tables equal to the first parse.
- `NhlGameModel.predict` gives the same result with the new tables empty and full.

## Delivery

1. Archive-backed parsers and tables, with fixtures for a modern game and a 2014-15 game. One pull request.
2. Stints and reconciliation. One pull request.
3. Refresh units and the backfill. One pull request.
4. The real backfill, and a report under `docs/research/` with the per-season count of `reconciled`, `provisional`, and `incomplete` games and the measured tolerances.

## Calls made

- The shared archive of #82 instead of the `raw_payloads` table of the issue body, so there is one payload scheme.
- A schema version bump instead of DDL on connect, because a consumer open must not run DDL (#80). This changes the issue-body call ("additive tables need no version bump") for that reason.
- `attack_x_sign` NULL when the feed has no side, instead of a period-parity guess, so a wrong direction cannot reach a shot model.
