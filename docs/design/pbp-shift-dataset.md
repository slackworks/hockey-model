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

A new schema version, through `hockey-model store upgrade`.
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
    game_state TEXT NOT NULL,                            -- the feed gameState, such as OFF or LIVE
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
    espn_id INTEGER NOT NULL, espn_name TEXT NOT NULL, toi INTEGER, pp_toi INTEGER, sh_toi INTEGER, es_toi INTEGER,
    shifts INTEGER,
    PRIMARY KEY (obs_id, espn_id)
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
The newest observation is the newest one of the game with the status `ok` or `shape_error`, by fetch time and then by observation id (view `latest_event_observations`).
A fetch that the parser refused is the newest one, so its game has no rows, and the view never serves an older fetch in its place.
A fetch that failed to answer (`http_error`, `store_error`) does not count.

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
6. The two teams of the ESPN summary are the two teams of the NHL game that the observation key names. The parser checks that the summary lists two teams, and the refresh step maps an ESPN event to an NHL game, so a wrong map would store rows under the wrong game. A mismatch marks the game `incomplete`. Tracked in #35.

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

## Built as

Delivery step 1 shipped with these changes from the plan above:

- `pbp_roster (obs_id, game_id, player_id, team, position)` holds the roster of the play-by-play, because the stint step needs the position `G`.
- `shifts` is keyed on `(obs_id, row_no)`, where `row_no` is the place of the shift among the shift rows of the feed. The feed can repeat a shift, and the stint step merges the repeats.
- A derived row has a `parser` column, and a rebuild replaces the rows of one observation in place. The archive keeps the bytes, so the earlier parse is not lost. A payload that the rebuild refuses leaves no rows.
- `espn_skater_toi` is keyed on `(obs_id, espn_id)` and has no `nhl_id`. The ESPN athlete id is in the payload, so two skaters with one display name on one team stay apart. A rebuild must give the rows of a first parse of the same bytes, and an id from the player catalog of the day would change it. The reconciliation joins the ESPN id and name to the roster of the game when it reads.
- `pbp_events` has a `game_state` column, the `gameState` of the feed. A partial in-game feed then differs from a final one in the tables. The reconciliation step reads it for `event_status`.
- The observation key of `espn_summary` is the NHL game id, through `Reading.key`.
- The version is 11. Schema version 10 is the boxscore revision tables of #82.
- The tables `stints` and `event_status`, the view `latest_stints`, and the stint builder return with the reconciliation step that writes them. They have no writer in step 1.
- A parser refuses an empty feed, a payload for another game or event, a repeated event, player, or skater, a clock past its limit, and an id outside the integer range of the store. An event type with no entry in the role map keeps its type and no players, and the archived payload keeps the player keys for a later parser.
- `store rebuild --source nhl_pbp`, `nhl_shifts`, or `espn_summary` rewrites the rows of each archived observation. `--allow-refused` lets a stricter parser drop a payload that an earlier parser accepted.

## Calls made

- The shared archive of #82 instead of the `raw_payloads` table of the issue body, so there is one payload scheme.
- A schema version bump instead of DDL on connect, because a consumer open must not run DDL (#80). This changes the issue-body call ("additive tables need no version bump") for that reason.
- `attack_x_sign` NULL when the feed has no side, instead of a period-parity guess, so a wrong direction cannot reach a shot model.

Delivery step 2 shipped with these changes from the plan above:

- The version is 12. `stints` and `event_status` are new tables, and `latest_stints` and `latest_event_status` give the rows of the newest pair of observations. A pair is the newest play-by-play and the newest shift chart of a game. A refused newer fetch leaves no rows in these views.
- `stints` holds `home_goalies` and `away_goalies` as JSON arrays, like the skaters, in place of one goalie column for each side. An empty net is an empty array, and two goalies on the ice at once, which the feed can give, are both kept.
- `store reconcile [--game ID]` builds the stints and stores the status of each game that has a play-by-play or a shift chart. It reads stored rows only and makes no request. A game with no rows in one of the two is skipped and named. A game that raises a store error is named in `failed`, the other games still run, and the exit code is 1.
- The status is `provisional` until both observations were read at or after the start of the game plus the last day of `REVISIT_AFTER_DAYS`. Then it is `reconciled` when every check passed, and `incomplete` otherwise.
- Checks 1, 2, 5, and 6 run. Checks 3 and 4 need `TOI_TOLERANCE_S` and `STRENGTH_TOI_TOLERANCE_S`, which come from the backfill of delivery step 4. Their constants and the checks land with that measurement.
- Four checks run beside the design list. `roster`: each player with a shift is in `pbp_roster` with a position code of C, L, R, D, or G, because the builder takes a goalie from the code G and counts any other player as a skater. `shift_lengths`: no shift ends before it starts, and a shift with no length is only counted. `game_state`: the play-by-play of a final game says FINAL or OFF, as one value, so a feed that was read in play cannot reconcile. `espn_teams` always leaves a note.
- A check fails when it checked nothing. The situation check fails when no event carries a situation code, and the player check fails when the newest boxscore revision is incomplete or mismatched. A pass of the situation check states how many events it read.
- A goal with no owner team counts under its own key, so the team check fails on it.
- `store rebuild` of a play-by-play, a shift chart, or an ESPN summary deletes the stints and the status that name its rows, and the next `store reconcile` makes them again.
- `store reconcile` writes the stints and the status of a game only after every check ran. A skipped game carries its reason. A named game that is skipped exits 1.
- Check 5 accepts an event when the stint that owns it, or the stint that starts at its second, has at least the skaters and at least the goalies that the situation code gives. The first digit of the code is the away goalie, and the last digit is the home goalie, so a shift chart with no goalie shift cannot reconcile. A faceoff after a penalty ends takes the code of the players after the change. On the saved game, three faceoffs need this rule.
- `event_status.notes` holds the result of every check.
- The ESPN team check (6) notes that it was skipped when no ESPN summary of the game is stored, and it fails when the newest summary was refused.
- A run of `store reconcile` for a game that raises a store error deletes the stints and the status of that game. No status means that the game is unknown, and the old status must not outlive inputs that no longer reconcile. A run that succeeds keeps the rows of the newest pair only, so the tables do not grow with each revisit.
- A game is settled when the play-by-play, the shift chart, and the newest boxscore revision were all read after the last revisit day.

Step 3 must keep the status in step with its inputs. The status depends on more than the key `(pbp_obs, shift_obs)` names: checks 1, 2, and 6 read the final score, the newest boxscore revision, and the newest ESPN summary. The key and the views do not name those, so a new boxscore revision, or a new ESPN fetch that is refused, leaves the old status in place until the next `store reconcile`. Step 3 keeps them in step this way: each refresh unit that writes a boxscore revision, a play-by-play, a shift chart, or an ESPN summary of a game calls `reconcile_game` for that game in the same transaction. The other option, to store the boxscore revision and the ESPN observation on `event_status` and name them in the views, adds two more key columns for what one call gives. Step 3 chooses the call, and its tests must show that a new boxscore revision changes a stored status.

Delivery step 3a shipped the refresh units of the play-by-play and the shift chart:

- A refresh step `events` follows the NHL step. One unit reads one final game and one source, in one transaction, with the rules of a boxscore read: a stop after three failed units in a row, a wait of a day after a failed read, and a known gap after the existing failure rule.
- A plain refresh reads the first copy of a game that started in the last `FIRST_READ_DAYS` (14) days. An older game with no events is the work of the backfill, in delivery step 3d.
- The revisit days are `REVISIT_AFTER_DAYS` of the boxscore, and the due query takes the source. A revisit is bounded by `MAX_REVISITS` for each source.
- The derive hook of each of the three units (boxscore, play-by-play, shift chart) writes the rows and then calls `reconcile_game` for the game, in the same transaction. A game that cannot reconcile has its stints and status removed, and the error goes to the report. The unit does not fail, because the bytes that it read are good and the archive keeps them.
- The ESPN summary unit has the same hook in delivery step 3b.
