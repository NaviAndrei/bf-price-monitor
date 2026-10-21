#!/usr/bin/env python3
"""
PreToolUse hook: venv_guard.py (bf-price-monitor project-scoped)
Tests, type checks and project scripts must run with the project venv. Plain
`python` / `py` resolve to the system Python, which lacks the project package
and its extras (past sessions hit `ModuleNotFoundError: bf_price_monitor`
over a hundred times). Blocks, with a message naming the right command:

  * `python|py -m pytest|mypy ...` and `python|py scripts/<name>.py ...`
    when the interpreter is not inside a .venv folder;
  * a bare `pytest`, `py.test` or `mypy` command.

Allowed: .venv/Scripts/python.exe in any form, `uv run ...`, other python
uses (`-c`, `-m py_compile`, `-m pip`), and quoted text such as an echoed
string. Command parsing is shared with branch_guard.py, so quoting, wrappers
(env, sudo) and shell -c / -Command unwrapping behave identically.
Exit code 2 = BLOCK (reason on stderr), 0 = ALLOW. Fail-open on parse errors.
"""

import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "branch_guard", Path(__file__).with_name("branch_guard.py")
)
branch_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(branch_guard)

VENV_PYTHON = ".venv/Scripts/python.exe"
PYTHON_NAME = re.compile(r"^(python[\d.]*|py)$")
BARE_TOOLS = {"pytest", "py.test", "mypy"}
GUARDED_MODULES = {"pytest", "mypy"}
SCRIPT_PATH = re.compile(r"^(\./)?scripts/[\w./-]+\.py$")
MAX_DEPTH = branch_guard.MAX_WRAPPER_DEPTH


def _in_venv(token):
    return ".venv/" in token.replace("\\", "/").lower()


def _message(what):
    return (
        f"BLOCKED: {what} would run with the system Python, which lacks the "
        f"project package and extras. Use {VENV_PYTHON} instead, for example "
        f"`{VENV_PYTHON} -m pytest ...`."
    )


def _check_python(tokens):
    args = tokens[1:]
    if "-m" in args:
        i = args.index("-m")
        module = args[i + 1] if i + 1 < len(args) else ""
        if module in GUARDED_MODULES:
            return _message(f"`-m {module}`")
        return None
    for token in args:
        if token.startswith("-"):
            continue
        if SCRIPT_PATH.match(token.replace("\\", "/")):
            return _message(f"`{token}`")
        break
    return None


def _check_segment(segment, depth=0):
    tokens = branch_guard._strip_prefixes(branch_guard._strip_script_block(segment))
    if not tokens or _in_venv(tokens[0]):
        return None
    program = branch_guard._program_name(tokens[0])
    if program in branch_guard.SHELLS:
        for i, token in enumerate(tokens[1:], 1):
            if token.lower() in branch_guard.SHELL_COMMAND_FLAGS and i + 1 < len(
                tokens
            ):
                if depth >= MAX_DEPTH:
                    return None  # branch_guard already blocks over-deep nesting
                rest = tokens[i + 1 :]
                one = len(rest) == 1 or program in branch_guard.ONE_STRING_SHELLS
                return evaluate(rest[0] if one else shlex.join(rest), depth + 1)
        return None
    if program in BARE_TOOLS:
        return _message(f"`{program}`")
    if PYTHON_NAME.match(program):
        return _check_python(tokens)
    return None


def evaluate(command, depth=0):
    """Return a block reason for `command`, or None when it is allowed."""
    try:
        segments = branch_guard.split_segments(command)
    except ValueError:
        return None
    for segment in segments:
        reason = _check_segment(segment, depth)
        if reason:
            return f"{reason} Command: {command[:150]}"
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict) or data.get("tool_name") not in (
        "Bash",
        "PowerShell",
    ):
        return 0
    tool_input = data.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    reason = evaluate(command)
    if reason:
        print(f"[venv_guard] {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
