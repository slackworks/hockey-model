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
def packaged_schema(file_name: str) -> dict[str, Any]:
    """Return the JSON Schema that ships beside this package, read once."""
    try:
        text = resources.files("hockey_model").joinpath(file_name).read_text(encoding="utf-8")
    except OSError as exc:
        raise HockeyModelError(
            f"cannot read the schema file {file_name} from the installed package: {exc}; "
            "reinstall the hockey-model wheel"
        ) from exc
    return json.loads(text)


def check_report(payload: Mapping[str, Any]) -> None:
    """Raise ``HockeyModelError`` unless ``payload`` is a daily report of this schema version."""
    validate_json(payload, packaged_schema(REPORT_SCHEMA_FILE), REPORT_SCHEMA_FILE)


def check_index(payload: Mapping[str, Any]) -> None:
    """Raise ``HockeyModelError`` unless ``payload`` is a build index of this schema version."""
    validate_json(payload, packaged_schema(INDEX_SCHEMA_FILE), INDEX_SCHEMA_FILE)


def version_file(report: Mapping[str, Any]) -> str:
    """Return the file name of one build: ``report-DATE-HHMMSSZ.json``, from its ``generated_at``.

    The report must pass ``check_report``, so ``generated_at`` has the fixed shape that the
    slices below read. This is the one place that names a build.
    """
    at = report["generated_at"]
    return f"report-{report['date']}-{at[11:13]}{at[14:16]}{at[17:19]}Z.json"


def index_entry(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return the index entry of one build."""
    model = report["model"]
    return {
        "file": version_file(report),
        "generated_at": report["generated_at"],
        "code_sha": model["code_sha"] if model else None,
        "fit_sha256": model["fit_sha256"] if model else None,
        "inputs_sha256": report["inputs_sha256"],
        "schema_version": report["schema_version"],
    }


def add_build(index: Mapping[str, Any] | None, report: Mapping[str, Any]) -> dict[str, Any]:
    """Return the index of a day with the build of ``report`` added.

    The report must pass ``check_report``. A build that is already listed under the same file
    name is replaced by its new entry. No other build is dropped. The builds stay in the order
    of ``generated_at``.
    """
    check_report(report)
    entry = index_entry(report)
    builds: list[dict[str, Any]] = []
    if index is not None:
        check_index(index)
        if index["date"] != report["date"]:
            raise HockeyModelError(
                f"index of {index['date']} cannot take a build of {report['date']}; "
                "pass the index of the same day"
            )
        builds = [b for b in index["builds"] if b["file"] != entry["file"]]
    builds.append(entry)
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


def write_index(report_path: Path, day: str, existing: Path | None, out: Path) -> tuple[int, str]:
    """Write the index with the build of ``report_path`` added.

    Return the number of builds and the file name of the added build. The report must belong
    to ``day``, the day of the release that receives the index.
    """
    index = _read_json(existing, "index") if existing is not None else None
    report = _read_json(report_path, "report")
    result = add_build(index, report)
    if report["date"] != day:
        raise HockeyModelError(
            f"report-index: the report {report_path} is for {report['date']}, not {day}; "
            "pass the report of the release day"
        )
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return len(result["builds"]), version_file(report)
