"""The published contract of the daily report: its schema, its version, and its build index.

sportsbettr reads the ``report-YYYY-MM-DD.json`` release assets. The two schema files sit
beside this module and ship in the wheel. ``docs/design/report-contract.md`` says when
``SCHEMA_VERSION`` changes.
"""

import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from hockey_model.json_schema import validate_json
from hockey_model.types import HockeyModelError

SCHEMA_VERSION = 1
REPORT_SCHEMA_FILE = "daily_report.schema.json"
INDEX_SCHEMA_FILE = "report_index.schema.json"


@cache
def _schema(file_name: str) -> dict[str, Any]:
    text = resources.files("hockey_model").joinpath(file_name).read_text(encoding="utf-8")
    return json.loads(text)


def check_report(payload: Mapping[str, Any]) -> None:
    """Raise ``HockeyModelError`` unless ``payload`` is a daily report of this schema version."""
    validate_json(payload, _schema(REPORT_SCHEMA_FILE), REPORT_SCHEMA_FILE)


def check_index(payload: Mapping[str, Any]) -> None:
    """Raise ``HockeyModelError`` unless ``payload`` is a build index of this schema version."""
    validate_json(payload, _schema(INDEX_SCHEMA_FILE), INDEX_SCHEMA_FILE)


def index_entry(report: Mapping[str, Any], file_name: str) -> dict[str, Any]:
    """Return the index entry of one build. ``file_name`` is the versioned report file."""
    model = report["model"]
    return {
        "file": file_name,
        "generated_at": report["generated_at"],
        "code_sha": model["code_sha"] if model else None,
        "fit_sha256": model["fit_sha256"] if model else None,
        "inputs_sha256": report["inputs_sha256"],
        "schema_version": report["schema_version"],
    }


def add_build(
    index: Mapping[str, Any] | None, report: Mapping[str, Any], file_name: str
) -> dict[str, Any]:
    """Return the index of a day with the build of ``report`` added.

    The report must pass ``check_report``. A build that is already listed under the same file
    name is replaced by its new entry. No other build is dropped. The builds stay in the order
    of ``generated_at``.
    """
    check_report(report)
    builds: list[dict[str, Any]] = []
    if index is not None:
        check_index(index)
        if index["date"] != report["date"]:
            raise HockeyModelError(
                f"index of {index['date']} cannot take a build of {report['date']}; "
                "pass the index of the same day"
            )
        builds = [b for b in index["builds"] if b["file"] != file_name]
    builds.append(index_entry(report, file_name))
    builds.sort(key=lambda build: (build["generated_at"], build["file"]))
    result = {"schema_version": SCHEMA_VERSION, "date": report["date"], "builds": builds}
    check_index(result)
    return result


def _read_json(path: Path, role: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HockeyModelError(
            f"report-index: cannot read the {role} {path}: {exc}; pass a JSON file"
        ) from exc


def write_index(report_path: Path, file_name: str, existing: Path | None, out: Path) -> int:
    """Write the index with the build of ``report_path`` added. Return the number of builds."""
    index = _read_json(existing, "index") if existing is not None else None
    result = add_build(index, _read_json(report_path, "report"), file_name)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return len(result["builds"])
