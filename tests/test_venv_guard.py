"""Tests for .claude/hooks/venv_guard.py (PreToolUse interpreter guard)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "venv_guard.py"
)
_spec = importlib.util.spec_from_file_location("venv_guard", HOOK_PATH)
venv_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(venv_guard)

BLOCKED = [
    "python -m pytest",
    "python -m pytest -x --tb=short",
    "python3 -m pytest tests/",
    "py -m mypy --strict bf_price_monitor/",
    "pytest -v",
    "mypy bf_price_monitor",
    "python scripts/scrape.py --dry-run",
    "python scripts/feedback.py summary",
    "cd x && python -m pytest",
    'bash -c "pytest -x"',
    'powershell -Command "python -m pytest"',
    "env PYTHONPATH=. python -m pytest",
    "C:\\Python312\\python.exe -m pytest",
]

ALLOWED = [
    ".venv/Scripts/python.exe -m pytest -x",
    ".\\.venv\\Scripts\\python.exe -m pytest",
    "D:\\Projects\\x\\.venv\\Scripts\\python.exe scripts/feedback.py summary",
    ".venv/Scripts/python.exe -m mypy --strict bf_price_monitor/",
    "uv run pytest",
    "uv run python -m pytest",
    "python -m py_compile scripts/scrape.py",
    "python -c 'print(1)'",
    "python -m pip list",
    "python --version",
    'echo "python -m pytest"',
    "git log --oneline -3",
    "ruff check .",
    "grep -rn pytest docs/",
    "ls scripts/",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_system_python_for_tests_and_scripts_is_blocked(command):
    reason = venv_guard.evaluate(command)
    assert reason is not None, command
    assert ".venv/Scripts/python.exe" in reason


@pytest.mark.parametrize("command", ALLOWED)
def test_venv_and_unrelated_commands_are_allowed(command):
    assert venv_guard.evaluate(command) is None, command


def test_unparseable_command_fails_open():
    assert venv_guard.evaluate("python -m pytest 'unterminated") is None


def _run_hook(payload):
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


def test_hook_exit_codes_through_stdin():
    blocked = _run_hook(
        {"tool_name": "PowerShell", "tool_input": {"command": "python -m pytest"}}
    )
    assert blocked.returncode == 2
    assert "[venv_guard]" in blocked.stderr
    ok = _run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": ".venv/Scripts/python.exe -m pytest"},
        }
    )
    assert ok.returncode == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Edit", "tool_input": {"command": "python -m pytest"}},
        {"tool_name": "Bash", "tool_input": "not a dict"},
        ["not", "a", "dict"],
    ],
)
def test_hook_ignores_other_tools_and_bad_payloads(payload):
    assert _run_hook(payload).returncode == 0
