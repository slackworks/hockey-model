"""Build the wheel, install it alone in a clean venv, and run a real offline prediction (#98).

Usage: uv run python3 scripts/smoke_wheel.py [--drop NAME]

--drop NAME removes every file called NAME from the wheel before the install.
The smoke test then MUST fail.
This is the disposable build probe.
It shows that the test catches a missing package-data file or a missing entry point.

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
FINISHED_ID = "2015020001"
SCHEDULED_ID = "2025020001"
TOLERANCE = 1e-9


class SmokeError(Exception):
    """A smoke check failed."""


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
    except OSError as error:
        raise SmokeError(f"run {' '.join(command)}: {error}") from error
    if result.returncode != 0:
        raise SmokeError(
            f"run {' '.join(command)}: exit {result.returncode}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result.stdout


def _build_wheel(out_dir: Path, env: dict[str, str]) -> Path:
    out_dir.mkdir()
    _run(["uv", "build", "--wheel", "--out-dir", str(out_dir), str(ROOT)], cwd=out_dir, env=env)
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise SmokeError(f"build the wheel: expected one wheel in {out_dir}, found {len(wheels)}")
    return wheels[0]


def _drop_from_wheel(wheel: Path, name: str) -> None:
    """Rewrite ``wheel`` without any member called ``name`` (or an entry_points.txt line)."""
    with zipfile.ZipFile(wheel) as source:
        members = [(info, source.read(info.filename)) for info in source.infolist()]
    kept = [(info, data) for info, data in members if Path(info.filename).name != name]
    if len(kept) == len(members):
        raise SmokeError(f"drop {name}: the wheel has no file with that name; pass a real name")
    wheel.unlink()
    with zipfile.ZipFile(wheel, "w") as target:
        for info, data in kept:
            target.writestr(info, data)


def _check_prediction(prediction: dict[str, object]) -> None:
    home, away = prediction["home_win"], prediction["away_win"]
    if not isinstance(home, float) or not isinstance(away, float):
        raise SmokeError(f"check the prediction: win chances are not numbers: {home!r}, {away!r}")
    if not (math.isfinite(home) and math.isfinite(away)) or abs(home + away - 1.0) > TOLERANCE:
        raise SmokeError(f"check the prediction: win chances {home} and {away} do not sum to 1")
    for side in ("home_goals", "away_goals"):
        goals = prediction[side]
        if not isinstance(goals, dict):
            raise SmokeError(f"check the prediction: {side} is not an object: {goals!r}")
        values = [goals[key] for key in ("low", "point", "high")]
        if not all(isinstance(v, float) and math.isfinite(v) for v in values):
            raise SmokeError(f"check the prediction: {side} has a value that is not finite")
        if not values[0] <= values[1] <= values[2]:
            raise SmokeError(f"check the prediction: {side} is not ordered low <= point <= high")
    if not prediction["missing"]:
        raise SmokeError("check the prediction: the sparse store must report missing inputs")


def _smoke(work: Path, wheel: Path, env: dict[str, str]) -> None:
    venv = work / "venv"
    _run(["uv", "venv", "--python", "3.14", str(venv)], cwd=work, env=env)
    python = venv / "bin" / "python"
    _run(["uv", "pip", "install", "--python", str(python), str(wheel)], cwd=work, env=env)
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
    out = _run([cli, "game", SCHEDULED_ID], cwd=work, env=seeded)
    _check_prediction(json.loads(out))
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
            _smoke(work, wheel, env)
        except SmokeError as error:
            print(f"smoke test failed: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
