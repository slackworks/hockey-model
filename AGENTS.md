# AGENTS.md

Hockey player stat and game prediction library. Python 3.14, managed with uv.

## Commands

- Install: `uv sync --locked`
- Run tests: `uv run pytest` (writes `lcov.info` and `coverage.json`)
- Per-module coverage floor: `uv run python3 scripts/check_per_module_coverage.py`
- Lint and format: `uv run ruff check .` and `uv run ruff format --check .`
- Model check: bash scripts/tlc.sh (TLC on specs/daily_prepare.tla; needs Java 11 or later)
- Type check: `uv run ty check`
- Wheel smoke test: `uv run python3 scripts/smoke_wheel.py`. It builds the wheel, installs it alone in a clean venv, and predicts one game from a seeded store. CI runs it in the `smoke` job. `--drop NAME` removes a file from the wheel, and the test must then fail.
- Workflow scan: `prek run zizmor --all-files`. CI runs the same version in the `zizmor` job. Keep the hook rev and `ZIZMOR_VERSION` in `ci.yml` equal.
- Mutation testing (advisory, slow): `uv run mutmut run`, then `uv run mutmut results`. It mutates the parsers and the rate math listed in `[tool.mutmut]`. CI runs it weekly in `.github/workflows/mutation.yml`, in shards listed in `scripts/mutation_shards.py`. Each module in `[tool.mutmut]` must sit in exactly one shard. `uv run python3 scripts/mutation_shards.py check` verifies that.
- Hooks: `prek install`, then `prek run --all-files`

MUST use `uv run python3`, not bare `python3`.

## Layout

- `src/hockey_model/`: the package.
- `tests/`: pytest tests. They mirror the package structure.
- `scripts/`: CI helpers.

## Rules

- Dependencies are pinned to a patch level (`~=`) in `pyproject.toml`. `uv.lock` is committed.
- GitHub Actions are pinned to a commit SHA with a version comment.
- Keep line length at 100, functions at 8 complexity or less, and functions at 5 arguments or less.
- Issues track planned work. Use the `v0.1.0` milestone for the first release.
- The daily report output MUST be updated when anything new becomes available, such as a new input source, a new model, or a new feature. Regenerate the report path and its tests in the same change.
