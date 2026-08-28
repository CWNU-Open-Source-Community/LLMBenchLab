"""Production log sources keep dynamic and exception values out of messages."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import runpy
import sys
from pathlib import Path

import pytest

from app.core.logging import SanitizedJsonFormatter

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical", "exception"})


def _logging_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOG_METHODS:
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "logger":
            calls.append(node)
    return calls


def test_all_production_log_messages_are_literal_and_argument_free() -> None:
    violations: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        for call in _logging_calls(path):
            line = getattr(call, "lineno", 0)
            if (
                not call.args
                or not isinstance(call.args[0], ast.Constant)
                or not isinstance(call.args[0].value, str)
            ):
                violations.append(f"{path.relative_to(APP_ROOT)}:{line}:non_literal_message")
            if len(call.args) != 1:
                violations.append(f"{path.relative_to(APP_ROOT)}:{line}:format_arguments")
            if isinstance(call.func, ast.Attribute) and call.func.attr == "exception":
                violations.append(f"{path.relative_to(APP_ROOT)}:{line}:logger_exception")
            if any(keyword.arg == "exc_info" for keyword in call.keywords):
                violations.append(f"{path.relative_to(APP_ROOT)}:{line}:exc_info")

    assert violations == []


def test_all_production_loggers_and_literal_structured_values_are_registered() -> None:
    violations: list[str] = []
    formatter = SanitizedJsonFormatter()
    for path in sorted(APP_ROOT.rglob("*.py")):
        calls = _logging_calls(path)
        if not calls:
            continue
        module_name = "app." + ".".join(path.relative_to(APP_ROOT).with_suffix("").parts)
        record = logging.LogRecord(
            name=module_name,
            level=logging.INFO,
            pathname=str(path),
            lineno=1,
            msg="Registered application log source",
            args=(),
            exc_info=None,
        )
        payload = json.loads(formatter.format(record))
        if payload["logger"] != module_name:
            violations.append(f"{path.relative_to(APP_ROOT)}:unregistered_logger")

        for call in calls:
            message = call.args[0]
            assert isinstance(message, ast.Constant) and isinstance(message.value, str)
            record.msg = message.value
            if json.loads(formatter.format(record))["message"] != message.value:
                violations.append(
                    f"{path.relative_to(APP_ROOT)}:{call.lineno}:unregistered_message"
                )
            for keyword in call.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
                    if (
                        not isinstance(key, ast.Constant)
                        or key.value not in {"event", "result", "error_code"}
                        or not isinstance(value, ast.Constant)
                        or not isinstance(value.value, str)
                    ):
                        continue
                    setattr(record, key.value, value.value)
                    rendered = json.loads(formatter.format(record))
                    if rendered.get(key.value) != value.value:
                        violations.append(
                            f"{path.relative_to(APP_ROOT)}:{call.lineno}:"
                            f"unregistered_{key.value}:{value.value}"
                        )
                    delattr(record, key.value)

    assert violations == []


def test_worker_module_entrypoint_keeps_registered_logger_without_running_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real ``python -m`` name without starting I/O or the event loop."""

    requested_logger_names: list[str | None] = []
    original_get_logger = logging.getLogger

    def capture_logger(name: str | None = None) -> logging.Logger:
        requested_logger_names.append(name)
        return original_get_logger(name)

    def close_worker_coroutine(coroutine) -> int:
        coroutine.close()
        return 0

    # A previously imported module would prevent runpy from reproducing the
    # ``__main__`` execution contract. MonkeyPatch restores it after the test.
    monkeypatch.delitem(sys.modules, "app.worker", raising=False)
    monkeypatch.setattr(logging, "getLogger", capture_logger)
    monkeypatch.setattr(asyncio, "run", close_worker_coroutine)

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("app.worker", run_name="__main__")

    assert exit_info.value.code == 0
    assert "app.worker" in requested_logger_names
    assert "__main__" not in requested_logger_names
