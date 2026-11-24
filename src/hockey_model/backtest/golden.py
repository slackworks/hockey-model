"""Hash the outputs of a backtest, so a change in speed shows that it moved no number.

``exact_hashes`` hashes the bytes of the two files. Two runs on one machine must match it. The
last binary digit of a float can differ between machines, so ``canonical_hashes`` rounds every
float first, and a hash that a test commits uses that form. Design: docs/design/backtest-speed.md
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hockey_model.backtest.game_records import GameRecord, write_game_records
from hockey_model.backtest.runner import BacktestReport
from hockey_model.game_model.logistic import LogisticFit
from hockey_model.store.timestamps import iso_utc

# A change in the solver stop tolerance moves a value at this scale, and a change of the model
# moves a reported value by about 1e-2. A last-bit difference between machines is far below it.
DECIMALS = 9


def report_text(report: BacktestReport) -> str:
    """Return the report as JSON text with sorted keys, as the golden check writes it."""
    return json.dumps(asdict(report), sort_keys=True, indent=2) + "\n"


def rounded(value: Any) -> Any:
    """Return ``value`` with every float rounded to ``DECIMALS``, through dicts and sequences."""
    if isinstance(value, float):
        return round(value, DECIMALS)
    if isinstance(value, dict):
        return {key: rounded(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [rounded(item) for item in value]
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def exact_hashes(report_path: Path, games_path: Path) -> dict[str, str]:
    """Return the SHA-256 of the bytes of the report file and of the games file."""
    return {
        "exact_report_sha256": _sha256(report_path.read_bytes()),
        "exact_games_sha256": _sha256(games_path.read_bytes()),
    }


def canonical_hashes(report: BacktestReport, records: Sequence[GameRecord]) -> dict[str, str]:
    """Return hashes of the report and the records with every float rounded to ``DECIMALS``."""
    rounded_report = json.dumps(rounded(asdict(report)), sort_keys=True)
    rows = [
        [
            record.game_id,
            iso_utc(record.start),
            record.home,
            record.away,
            record.run,
            round(record.home_win_prob, DECIMALS),
            record.home_won,
        ]
        for record in records
    ]
    return {
        "canonical_report_sha256": _sha256(rounded_report.encode("utf-8")),
        "canonical_games_sha256": _sha256(json.dumps(rows).encode("utf-8")),
        "decimals": str(DECIMALS),
    }


def canonical_fit_hash(fit: LogisticFit) -> str:
    """Return the hash of the weights of a fit, with every float rounded to ``DECIMALS``.

    The diagnostics describe the fit and change no prediction, so the hash leaves them out.
    """
    values = {
        "features": list(fit.features),
        "intercept": fit.intercept,
        "elo_weight": fit.elo_weight,
        "weights": dict(fit.weights),
    }
    return _sha256(json.dumps(rounded(values), sort_keys=True).encode("utf-8"))


def write_golden_files(
    report: BacktestReport, records: Sequence[GameRecord], out_dir: Path
) -> dict[str, str]:
    """Write ``report.json``, ``games.csv``, and ``hashes.json`` to ``out_dir``.

    Return the exact and the canonical hashes in one dict. The caller pastes both in a pull
    request. The key names say which form each hash has.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path, games_path = out_dir / "report.json", out_dir / "games.csv"
    report_path.write_text(report_text(report), encoding="utf-8")
    write_game_records(games_path, records)
    hashes = {**exact_hashes(report_path, games_path), **canonical_hashes(report, records)}
    (out_dir / "hashes.json").write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    return hashes
