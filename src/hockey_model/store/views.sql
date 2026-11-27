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

-- The rows of the newest observation of each game (#35). A rebuild or a revisit adds a newer
-- observation, and the older rows stay in the table.
DROP VIEW IF EXISTS latest_pbp_events;
CREATE VIEW latest_pbp_events AS
SELECT e.* FROM pbp_events e
JOIN (SELECT game_id, MAX(obs_id) AS obs_id FROM pbp_events GROUP BY game_id) n
    ON e.obs_id = n.obs_id;

DROP VIEW IF EXISTS latest_shifts;
CREATE VIEW latest_shifts AS
SELECT s.* FROM shifts s
JOIN (SELECT game_id, MAX(obs_id) AS obs_id FROM shifts GROUP BY game_id) n
    ON s.obs_id = n.obs_id;
