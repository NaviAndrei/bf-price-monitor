#!/usr/bin/env python3
"""
PreToolUse hook: branch_guard.py (bf-price-monitor project-scoped)
Solo-dev policy for this repo: all work happens directly on `master`.
Blocks creating new branches, and blocks commits/pushes made from any
branch other than `master`.
Exit code 2 = BLOCK, Exit code 0 = ALLOW
"""
import sys, json, re, subprocess

ALLOWED_BRANCHES = {"master", "master-fkamih"}

try:
    input_data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if input_data.get("tool_name", "") not in ("Bash", "PowerShell"):
    sys.exit(0)

command = input_data.get("tool_input", {}).get("command", "")
cwd = input_data.get("cwd", "") or "."


def block(reason):
    print(json.dumps({"decision": "block", "reason": f"[branch_guard] {reason}"}))
    sys.exit(2)


BRANCH_CREATE_PATTERNS = [
    r"git\s+checkout\s+-b\s+\S+",
    r"git\s+switch\s+-c\s+\S+",
    r"git\s+worktree\s+add\s+.*-b\s+\S+",
    r"git\s+branch\s+(?!-d\b|-D\b|--delete\b|-a\b|-v\b|-r\b|-l\b|--list\b|-m\b|--show-current\b)\S+",
]
for pattern in BRANCH_CREATE_PATTERNS:
    if re.search(pattern, command, re.IGNORECASE):
        block(
            f"BLOCKED: creating a new branch is disabled on this repo — all "
            f"work goes directly on an allowed branch ({', '.join(sorted(ALLOWED_BRANCHES))}). Command: {command[:150]}"
        )

if re.search(r"\bgit\s+commit\b", command, re.IGNORECASE) or re.search(r"\bgit\s+push\b", command, re.IGNORECASE):
    try:
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        current = ""
    if current and current not in ALLOWED_BRANCHES:
        allowed = ", ".join(f"'{b}'" for b in sorted(ALLOWED_BRANCHES))
        block(
            f"BLOCKED: currently on branch '{current}'. Commits and pushes are "
            f"only allowed on: {allowed}. Switch to one of these first."
        )

sys.exit(0)
