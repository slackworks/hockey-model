# ESPN fixtures

Captured on 2026-11-03 with one request each.

- `scoreboard_20261103.json` and `scoreboard_20261104.json`: the scoreboard for two days with games not started. They hold `Confirmed` and `Expected` goalies and the `TB`, `NJ`, `LA`, and `SJ` codes.
- `summary_401803060.json`: the game summary of EDM and LA on 2026-01-10 (NHL game 2025020709). Only `boxscore.players` keeps its athletes, trimmed to `id` and `displayName`. The market keys `pickcenter`, `odds`, and `againstTheSpread` stay (one item each), so a test can show that the archive strips them.
- `injuries.json`: the injury list of five clubs (ANA, LA, SJ, TB, NJ). The athlete `links`, `headshot`, and team `logos` keys and the `longComment` text are removed to cut the file size. No parser reads them.
