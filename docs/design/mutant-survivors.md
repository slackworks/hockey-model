# Surviving mutants (#31)

## Goal

Every surviving mutant in the `only_mutate` modules of `[tool.mutmut]` is killed by a test, or carries a `# pragma: no mutate` with a reason.
The owner decision and the work plan are in the issue body. This design adds what a builder needs before the first commit.

## Before the first commit

1. Run `trash mutants` when the directory exists, then `uv run mutmut run`, then `uv run mutmut results`. Write the count of each module in the issue. The counts in the issue body are out of date.
2. The modules are now: `backtest/metrics.py`, `experiments/models.py`, `game_model/elo.py`, `game_model/logistic.py`, `json_schema.py`, and `sources/*.py`. Read `pyproject.toml` again, because the list can change.
3. Prove the pragma form on a scratch copy: add `# pragma: no mutate` to one line of a copy of a small module in `.tmp/`, point a scratch `[tool.mutmut]` at it, and check that `mutmut` makes no mutant of that line. Write the form that worked in the first commit message.
4. Run `uv run mutmut run "hockey_model.<module>.*"` for one module at a time. The pattern form is the one `scripts/mutation_shards.py` prints.

## Sort each survivor

Run `uv run mutmut show <name>` and put the survivor in one of three groups.

| Group | Sign | Fix |
| --- | --- | --- |
| Message | The mutant changes the text of an error that a caller sees. | Assert the full message: `pytest.raises(SourceShapeError, match=f"^{re.escape(message)}$")`. |
| Logic | The mutant changes a comparison, a bound, a default, or an operator. | Write a test. Run it against the mutant (`mutmut apply <name>`, then the test, then `git checkout` of the file) and see it fail. Keep it only when it fails there and passes on the original. |
| Equivalent | No input can tell the mutant from the original: `zip(..., strict=True)` safety nets, a rounding guard, a solver branch that no input reaches, `_LOG.*` text. | Add `# pragma: no mutate  # <reason>` on the line. The reason names why no input reaches it. |

A timeout counts as a kill only when an endless loop is the expected failure of the mutant. Otherwise find the cause and write it in the commit.

## Order and commits

One branch for the issue.
One commit for each module, in the order of the issue body.
Each commit body gives the survivor count of the module before and after.
Keep each test module under its 750-line cap. Split a test module by topic when it grows past the cap.

## Traps

The issue body lists them. Two more:

- `mutmut apply` changes the source file. Run `git diff --stat src/` before each commit, and see only the files that the commit means to change.
- A full-message assertion on a message that holds a URL or a path MUST build the expected text from the same constant that the code uses, so a test does not copy a number or a URL (owner rule: no copied numbers in prose or tests that duplicate config).

## Done when

The done list of the issue body holds.
Also write the final counts as JSON in `docs/mutation-baseline.json`: `{"commit": "...", "modules": {"sources.nhl": {"killed": n, "survived": 0, "pragma": n}}}`, so #75 and #76 read a number and not a log.

## Survivors of the backtest modules that no test can kill (#288)

The modules `backtest.golden`, `backtest.runner`, `backtest.shards`, and `backtest.units` joined `only_mutate` in #288.
`backtest.runner` has no survivor.
The survivors below stay on purpose, and each row gives the reason.
A survivor that is not in this list is a gap in the tests.
A mutant that ends in `timeout` counts as detected.

### `backtest.golden`

| Mutants | Change | Reason no test can kill it |
| --- | --- | --- |
| `canonical_hashes` 20, 27; `canonical_fit_hash` 21; `write_golden_files` 20, 49 | `"utf-8"` becomes `"UTF-8"` | Python reads both spellings as the same codec. The bytes are equal. |
| `write_golden_files` 15, 17, 35, 37 | The `encoding` argument is removed or is `None` | `json.dumps` escapes every non-ASCII character, so the text is ASCII. ASCII gives the same bytes in each encoding that the locale can select. |

### `backtest.units`

| Mutants | Change | Reason no test can kill it |
| --- | --- | --- |
| `open_read_only` 2, 4, 6 | `uri=True` becomes `None`, is removed, or becomes `False` | The SQLite build of the test machine reads `file:` names as URIs with or without the flag. |
| `database_path` 8, 9 | The case of `PRAGMA database_list` changes | SQL keywords do not depend on case. |

### `backtest.shards`

| Mutants | Change | Reason no test can kill it |
| --- | --- | --- |
| `build_season_rows` 11 | The goalie mode is `None` | The run does not finish, so mutmut reports a timeout. This counts as detected. |

### When a new survivor appears

1. Run `uv run mutmut show <name>` and read the change.
2. Write a test that fails for the change. Run the module again.
3. When no test can fail for the change, add one row here with the reason.
