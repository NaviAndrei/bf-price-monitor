#!/usr/bin/env python3
"""
PostToolUse hook: py_compile_check.py (bf-price-monitor project-scoped)
Runs `python -m py_compile` on the file an Edit/Write tool call just
touched, but only when that file ends in `.py` -- Markdown/JSON writes
(docs, watchlist.json, price_history.json) are the majority of edits in
this repo and gain nothing from a syntax check, so this exits immediately
for anything else instead of spawning a subprocess.
Exit code 2 = surfaces a compile error back to the model, exit 0 = silent.
"""
import json
import subprocess
import sys

try:
    input_data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if not isinstance(input_data, dict):
    sys.exit(0)

if input_data.get("tool_name", "") not in ("Edit", "Write"):
    sys.exit(0)

tool_input = input_data.get("tool_input", {})
if not isinstance(tool_input, dict):
    sys.exit(0)

target_file = tool_input.get("file_path", "")
if not target_file.endswith(".py"):
    sys.exit(0)

result = subprocess.run(
    [sys.executable, "-m", "py_compile", target_file],
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    print(
        f"[py_compile_check] {target_file} failed to compile:\n{result.stderr}",
        file=sys.stderr,
    )
    sys.exit(2)

sys.exit(0)
