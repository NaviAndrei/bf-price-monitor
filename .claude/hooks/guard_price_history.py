#!/usr/bin/env python3
"""
PreToolUse hook: guard_price_history.py (bf-price-monitor project-scoped)
data/price_history.json is written only by scripts/scrape.py's own
load_history()/save_history() functions, which own the schema version and
the idempotency/history-limit guarantees. Blocks any Edit/Write tool call
that targets that file directly, so a hand-edit can't corrupt or desync
200+ products of recorded price history.
Exit code 2 = BLOCK, Exit code 0 = ALLOW
"""
import json
import sys
from pathlib import PurePath

GUARDED_FILE = "data/price_history.json"

try:
    input_data = json.load(sys.stdin)
except Exception:
    # Malformed/absent stdin isn't something this hook can evaluate — fail
    # open rather than block an unrelated tool call on a parsing error.
    sys.exit(0)

if not isinstance(input_data, dict):
    sys.exit(0)

if input_data.get("tool_name", "") not in ("Edit", "Write"):
    sys.exit(0)

tool_input = input_data.get("tool_input", {})
if not isinstance(tool_input, dict):
    sys.exit(0)

target_file = tool_input.get("file_path", "")
if not target_file:
    sys.exit(0)

# PurePath(...).as_posix() normalizes Windows backslashes (data\price_history.json)
# and mixed separators to forward slashes before the substring check, so the
# guard fires the same way regardless of which OS produced the path.
normalized = PurePath(target_file).as_posix()

if normalized.endswith(GUARDED_FILE):
    print(
        f"[guard_price_history] BLOCKED: {target_file} is written only by "
        "scripts/scrape.py's load_history()/save_history() -- direct edits "
        "can desync the schema version or corrupt recorded history. Run the "
        "scraper or edit scripts/scrape.py instead.",
        file=sys.stderr,
    )
    sys.exit(2)

sys.exit(0)
