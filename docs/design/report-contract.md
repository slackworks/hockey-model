# The published report contract

## Problem

sportsbettr reads the `reports-DATE` release assets, because it cannot run the daily update.
The report JSON had no version and no schema.
A renamed field failed in the consumer with no signal.
A consumer that archives the forecasts as issued must tell a real change in the report from a broken one (#257).

## What is published

- `report-DATE.json` is the newest build of the day.
- `report-DATE-HHMMSSZ.json` is one build, named by its `generated_at` time. A published version is never replaced.
- `index-DATE.json` lists every build of the day, oldest first.

The schemas are `src/hockey_model/daily_report.schema.json` and `src/hockey_model/report_index.schema.json`.
They sit in the package, so the wheel carries them and a consumer can read them from the installed package.
`additionalProperties` is false at every object level, so a new field forces a change to the schema file in the same commit.

## When `schema_version` changes

`schema_version` is an integer, and it starts at 1.
The version rises by one in a change that does one of these:

1. It removes a field or renames a field.
2. It changes the type of a field, or the set of values that an enumeration allows a consumer to read.
3. It changes the meaning of a field, such as the unit or the time basis.

A new field does not raise the version when the schema marks it optional, which means it is not in `required`.
The game field `win_interval` is the first such field (#212). The report always writes it, and a consumer reads a missing field as null.
A consumer that reads a version it does not know MUST stop and name the version.
A change that raises the version MUST say so in `CHANGELOG.md`.

The schema holds `schema_version` as a constant.
A report with another version fails the check, so a raise needs a new schema file content in the same commit.
`inputs_sha256` covers `schema_version`, so a build with a new shape publishes again even when no input changed.

## Where the check runs

1. `daily` (the `write_report` function) builds the JSON, checks it against the schema, and only then writes a file. An invalid report exits non-zero, names the JSON path and the schema, and leaves no file.
2. `scripts/publish-daily-report.sh` runs `report-index`. That command checks the report again, and it checks the index before and after the new build. A failure stops the script before any upload.

## The index

An entry holds `file`, `generated_at`, `code_sha`, `fit_sha256`, `inputs_sha256`, and `schema_version`.
`report_schema.version_file` names a build from its `generated_at` time, and it is the one place that does so.
The publish script reads the name from the output of `report-index`.
The script downloads the index that is already on the release, and adds the new build.
The script reads the release first, builds the index, and checks both version names before it uploads any file, so a name collision leaves no half-published build.
The `publish` job runs one at a time for each day (`hockey-model-publish-DAY`), because the index is a read-modify-write of one release asset.
A publish that waits behind another is replaced.
While the day has no release, the next hourly run builds and publishes that report again.
After the first puck drop, the newer build stands for the day and the replaced build leaves no entry in the index, because the gate publishes no report for a published day.
Only the script step holds the release token, and `uv` does not sync there.
A build that is listed under the same file name is replaced by its new entry, so a retry of one build is safe.
No other entry is dropped.
A release that was published before the index existed gets an index with the first build that follows.
Its older builds have no entry, because their files have no `schema_version` and fail the report schema.

A consumer picks the newest build before a cutoff with the `generated_at` field, and does not parse file names.

## The validator

The check uses `src/hockey_model/json_schema.py`, the same validator as the experiment registry (#119).
It matches a `pattern` against the whole string.
It refuses a schema with a keyword or a format that nothing enforces.
It refuses data that is not plain JSON, such as NaN.
The report schema writes timestamps as patterns, because the `date-time` format needs an optional package.

## Options not taken

- A schema in a separate file tree outside the package. The wheel would not carry it, and a consumer could not find the version that matches its install.
- A second validator for the report. One validator keeps one set of rules for patterns and formats.
