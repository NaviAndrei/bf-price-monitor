#!/usr/bin/env python3
"""
PostToolUse hook: ruff_format.py (bf-price-monitor project-scoped)
Runs `ruff format` on the .py file an Edit/Write call just touched.
Deliberately does NOT run `ruff check --fix`: CLAUDE.md documents that
auto-fix strips import-only edits added before their usage lands, which
silently breaks multi-step edits. Lint stays an explicit `ruff check .`.
Never blocks: always exits 0 (py_compile_check.py owns syntax errors).
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    input_data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if not isinstance(input_data, dict) or input_data.get("tool_name") not in (
    "Edit",
    "Write",
):
    sys.exit(0)

tool_input = input_data.get("tool_input", {})
target_file = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
if not target_file.endswith(".py"):
    sys.exit(0)

cwd = input_data.get("cwd") or "."
venv_python = Path(cwd) / ".venv" / "Scripts" / "python.exe"
if venv_python.is_file():
    ruff = [str(venv_python), "-m", "ruff"]  # same ruff as `ruff format --check` in CI
elif shutil.which("ruff"):
    ruff = [shutil.which("ruff")]
else:
    ruff = [sys.executable, "-m", "ruff"]
try:
    subprocess.run(ruff + ["format", target_file], capture_output=True, timeout=20)
except (OSError, subprocess.SubprocessError):
    pass
sys.exit(0)
