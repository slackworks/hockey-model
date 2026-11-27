-- The newest read of each key. The tables are append-only, so a query that wants the
-- current value of a game, a team game, a goalie game, or a player selects from a view.
-- A connection drops and creates the views each time, so a changed definition reaches an
-- existing store without a schema version change.

DROP VIEW IF EXISTS latest_games;
CREATE VIEW latest_games AS
SELECT g.* FROM games g
JOIN (SELECT game_id, MAX(fetched_at) AS latest FROM games GROUP BY game_id) n
    ON g.game_id = n.game_id AND g.fetched_at = n.latest;

-- The states are the NHL feed states of a finished game: FINAL_STATES in sources/nhl.py.
DROP VIEW IF EXISTS latest_final_games;
CREATE VIEW latest_final_games AS
SELECT g.* FROM games g
JOIN (SELECT game_id, MAX(fetched_at) AS latest FROM games
      WHERE state IN ('FINAL', 'OFF') GROUP BY game_id) n
    ON g.game_id = n.game_id AND g.fetched_at = n.latest;

DROP VIEW IF EXISTS latest_mp_team_games;
CREATE VIEW latest_mp_team_games AS
SELECT m.* FROM mp_team_games m
JOIN (SELECT game_id, team, MAX(fetched_at) AS latest FROM mp_team_games
      GROUP BY game_id, team) n
    ON m.game_id = n.game_id AND m.team = n.team AND m.fetched_at = n.latest;

DROP VIEW IF EXISTS latest_mp_goalie_games;
CREATE VIEW latest_mp_goalie_games AS
SELECT m.* FROM mp_goalie_games m
JOIN (SELECT game_id, nhl_id, MAX(fetched_at) AS latest FROM mp_goalie_games
      GROUP BY game_id, nhl_id) n
    ON m.game_id = n.game_id AND m.nhl_id = n.nhl_id AND m.fetched_at = n.latest;

DROP VIEW IF EXISTS latest_players;
CREATE VIEW latest_players AS
SELECT p.* FROM players p
JOIN (SELECT nhl_id, MAX(fetched_at) AS latest FROM players GROUP BY nhl_id) n
    ON p.nhl_id = n.nhl_id AND p.fetched_at = n.latest;

-- The newest observation of each event source and key that the feed answered (#35). A fetch
-- that the parser refused is the newest one, and its game has no rows. A fetch that failed to
-- answer does not count. A revisit adds an observation. A rebuild rewrites the rows of an
-- observation in place. The fetch time orders the observations, because an import can insert an
-- old fetch after a new one.
DROP VIEW IF EXISTS latest_event_observations;
CREATE VIEW latest_event_observations AS
SELECT o.obs_id FROM observations o
WHERE o.source IN ('nhl_pbp', 'nhl_shifts')
  AND o.key IS NOT NULL
  AND o.status IN ('ok', 'shape_error')
  AND NOT EXISTS (
    SELECT 1 FROM observations n
    WHERE n.source = o.source AND n.key = o.key
      AND n.status IN ('ok', 'shape_error')
      AND (n.fetched_at > o.fetched_at OR (n.fetched_at = o.fetched_at AND n.obs_id > o.obs_id))
  );

DROP VIEW IF EXISTS latest_pbp_events;
CREATE VIEW latest_pbp_events AS
SELECT e.* FROM pbp_events e JOIN latest_event_observations l ON e.obs_id = l.obs_id;

DROP VIEW IF EXISTS latest_shifts;
CREATE VIEW latest_shifts AS
SELECT s.* FROM shifts s JOIN latest_event_observations l ON s.obs_id = l.obs_id;
