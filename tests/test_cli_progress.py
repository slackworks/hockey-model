"""Tests for the progress lines of the store commands (#133)."""

import json
import logging

import pytest

from hockey_model import progress, store_cli
from hockey_model.cli import main


def _fake(payload: object, code: int = 0):
    def handler(args: object) -> tuple[object, int]:
        progress.advance(2)
        return payload, code

    return handler


@pytest.mark.parametrize("command", ["refresh", "fit", "backtest", "daily", "report-due"])
def test_a_long_command_logs_its_start_and_end_on_stderr_and_keeps_stdout_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.setitem(store_cli._HANDLERS, command, _fake({"ok": True}))
    assert main([command, *_REQUIRED.get(command, [])]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True}
    assert captured.out == '{"ok": true}\n'
    lines = captured.err.splitlines()
    assert lines[0] == f"progress {command} run start elapsed=0.0s"
    assert lines[-1].startswith(f"progress {command} run end elapsed=")
    assert lines[-1].endswith("done=2")


_REQUIRED = {"daily": ["--out-dir", "."]}


def test_a_short_command_logs_no_progress(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(store_cli._HANDLERS, "report-decide", _fake({"ok": True}))
    args = ["report-decide", "--due", "true", "--games", "1", "--date", "2026-11-04"]
    assert main(args) == 0
    assert capsys.readouterr().err == ""


def test_a_failed_command_logs_a_stop_line_before_its_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(args: object) -> tuple[object, int]:
        raise ValueError("bad input")

    monkeypatch.setitem(store_cli._HANDLERS, "fit", boom)
    assert main(["fit"]) == 2
    err = capsys.readouterr().err.splitlines()
    assert err[1].startswith("progress fit run stop elapsed=")
    assert err[1].endswith("reason=ValueError")
    assert err[-1] == "error: bad input"


def test_main_removes_its_handler_when_it_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(store_cli._HANDLERS, "fit", _fake({"ok": True}))
    main(["fit"])
    assert progress.LOGGER.handlers == []
    assert progress.LOGGER.propagate is True
    assert progress.LOGGER.level in (logging.NOTSET, logging.INFO)


def test_a_command_that_returns_a_failure_code_ends_with_a_stop_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(store_cli._HANDLERS, "refresh", _fake({"errors": ["x"]}, code=1))
    assert main(["refresh"]) == 1
    last = capsys.readouterr().err.splitlines()[-1]
    assert " refresh run stop elapsed=" in last
    assert last.endswith(" reason=exit code 1")


def test_the_long_commands_are_commands_and_the_quiet_ones_are_named() -> None:
    assert store_cli._LONG_COMMANDS <= set(store_cli._HANDLERS)
    quiet = set(store_cli._HANDLERS) - store_cli._LONG_COMMANDS
    assert quiet == {"game", "report-decide", "report-compare", "report-index", "report-summary"}
