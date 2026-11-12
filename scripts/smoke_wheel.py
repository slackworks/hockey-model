"""Build the wheel, install it alone in a clean venv, and run a real offline prediction (#98).

Usage: uv run python3 scripts/smoke_wheel.py [--drop NAME]

--drop NAME removes every file called NAME from the wheel before the install.
This is the disposable build probe.
It shows that the smoke checks catch a missing package-data file or a missing entry point.

Exit code 0 means every smoke check passed.
Exit code 1 means a smoke check failed. With --drop, this is the result the probe expects.
Exit code 2 means the setup failed (build, venv, install, or an unexpected error).
The checks did not run.

The test runs from a temporary directory with PYTHONPATH cleared, so the checkout cannot leak in.
It makes no network call. It never runs `refresh`.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_SCRIPT = ROOT / "scripts" / "smoke_seed.py"
SCHEDULED_ID = "2025020001"
TOLERANCE = 1e-9
GOAL_KEYS = ("low", "point", "high")
# The seeded store has no goalie start and no MoneyPuck row, so the model must say so.
EXPECTED_MISSING = {"goalie_unknown", "xg_form"}


class SmokeError(Exception):
    """A smoke step failed. ``setup`` is true when the checks could not run at all."""

    def __init__(self, message: str, *, setup: bool = False) -> None:
        super().__init__(message)
        self.setup = setup


def _run(command: list[str], *, cwd: Path, env: dict[str, str], setup: bool = False) -> str:
    """Run ``command`` and return its stdout. The stderr of a passing command is dropped."""
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
    except OSError as os_error:
        raise SmokeError(f"run {' '.join(command)}: {os_error}", setup=setup) from os_error
    if result.returncode != 0:
        raise SmokeError(
            f"run {' '.join(command)}: exit {result.returncode}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}",
            setup=setup,
        )
    return result.stdout


def _build_wheel(out_dir: Path, env: dict[str, str]) -> Path:
    out_dir.mkdir()
    _run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir), str(ROOT)],
        cwd=out_dir,
        env=env,
        setup=True,
    )
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise SmokeError(
            f"build the wheel: expected one wheel in {out_dir}, found {len(wheels)}", setup=True
        )
    return wheels[0]


def _drop_from_wheel(wheel: Path, name: str) -> None:
    """Rewrite ``wheel`` without every member whose file name is ``name``."""
    with zipfile.ZipFile(wheel) as source:
        members = [(info, source.read(info.filename)) for info in source.infolist()]
    kept = [(info, data) for info, data in members if Path(info.filename).name != name]
    if len(kept) == len(members):
        raise SmokeError(
            f"drop {name}: the wheel has no file with that name; pass a real name", setup=True
        )
    # A rewrite to a side path keeps the original wheel whole if the write fails.
    side = wheel.with_suffix(".tmp")
    with zipfile.ZipFile(side, "w") as target:
        for info, data in kept:
            target.writestr(info, data)
    side.replace(wheel)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SmokeError(f"check the prediction: {label} is not a number: {value!r}")
    if not math.isfinite(value):
        raise SmokeError(f"check the prediction: {label} is not finite: {value!r}")
    return float(value)


def _field(data: object, key: str, label: str) -> object:
    if not isinstance(data, dict) or key not in data:
        raise SmokeError(f"check the prediction: {label} has no field {key!r}")
    return data[key]


def _check_goals(prediction: object, side: str) -> None:
    goals = _field(prediction, side, "the prediction")
    low, point, high = (_number(_field(goals, k, side), f"{side}.{k}") for k in GOAL_KEYS)
    if not low <= point <= high:
        raise SmokeError(f"check the prediction: {side} is not ordered low <= point <= high")


def _check_prediction(prediction: object) -> None:
    """Raise SmokeError unless ``prediction`` is a sound game prediction of the sparse store."""
    home = _number(_field(prediction, "home_win", "the prediction"), "home_win")
    away = _number(_field(prediction, "away_win", "the prediction"), "away_win")
    if abs(home + away - 1.0) > TOLERANCE:
        raise SmokeError(f"check the prediction: win chances {home} and {away} do not sum to 1")
    _check_goals(prediction, "home_goals")
    _check_goals(prediction, "away_goals")
    missing = _field(prediction, "missing", "the prediction")
    if not isinstance(missing, list):
        raise SmokeError(f"check the prediction: missing is not a list: {missing!r}")
    kinds = {str(_field(item, "kind", "a missing input")) for item in missing}
    if not EXPECTED_MISSING <= kinds:
        raise SmokeError(
            f"check the prediction: the sparse store must report {sorted(EXPECTED_MISSING)} "
            f"as missing, got {sorted(kinds)}"
        )


def _parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise SmokeError(
            f"parse the game output as JSON: {error}; output: {text[:200]!r}"
        ) from error


def _install(work: Path, wheel: Path, env: dict[str, str]) -> Path:
    venv = work / "venv"
    _run(["uv", "venv", "--python", "3.14", str(venv)], cwd=work, env=env, setup=True)
    python = venv / "bin" / "python"
    _run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        cwd=work,
        env=env,
        setup=True,
    )
    return venv


def _smoke(work: Path, venv: Path, env: dict[str, str]) -> None:
    python = venv / "bin" / "python"
    where = _run(
        [str(python), "-I", "-c", "import hockey_model; print(hockey_model.__file__)"],
        cwd=work,
        env=env,
    ).strip()
    if not Path(where).is_relative_to(venv):
        raise SmokeError(f"locate the package: {where} is outside the clean venv {venv}")
    print(f"installed package: {where}")
    cli = str(venv / "bin" / "hockey-model")
    _run([cli, "--help"], cwd=work, env=env)
    store = work / "store.db"
    seeded = {**env, "HOCKEY_MODEL_DB": str(store)}
    _run([str(python), "-I", str(SEED_SCRIPT), str(store)], cwd=work, env=seeded)
    _run([cli, "fit", "--through-season", "20152016", "--features", ""], cwd=work, env=seeded)
    _check_prediction(_parse_json(_run([cli, "game", SCHEDULED_ID], cwd=work, env=seeded)))
    print("smoke test passed")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--drop", metavar="NAME", help="remove this file from the wheel first")
    args = parser.parse_args(argv)
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "VIRTUAL_ENV"}}
    with tempfile.TemporaryDirectory(prefix="hockey-smoke-") as tmp:
        work = Path(tmp)
        try:
            wheel = _build_wheel(work / "dist", env)
            if args.drop:
                _drop_from_wheel(wheel, args.drop)
            venv = _install(work, wheel, env)
            _smoke(work, venv, env)
        except SmokeError as error:
            stage = "setup failed" if error.setup else "failed"
            print(f"smoke test {stage}: {error}", file=sys.stderr)
            return 2 if error.setup else 1
        except Exception as error:  # noqa: BLE001 - any other error is a setup failure, exit 2
            print(f"smoke test setup failed: {type(error).__name__}: {error}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
