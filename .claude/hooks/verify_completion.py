#!/usr/bin/env python3
"""
Stop hook: verify_completion.py (bf-price-monitor project-scoped)
Self-verification when Claude finishes a reply. Blocks (exit 2, stderr fed
back to Claude) when recent Python work is unverified:
  1. the latest commit touched code (scripts/, bf_price_monitor/, tests/,
     .github/) but not docs/progress.md -- the handoff log CLAUDE.md requires;
  2. `pytest` does not exit 0.
Stop fires after EVERY reply, so the checks only run when there is recent
work: HEAD was committed within RECENT_COMMIT_MINUTES, or tracked Python
files have uncommitted changes. Otherwise (Q&A, read-only turns) it exits 0.
Exit 1 would be a non-blocking error in Claude Code, so failures use exit 2.
`stop_hook_active` guards against an endless block loop.
"""

import json
import subprocess
import sys
import time

RECENT_COMMIT_MINUTES = 20
PYTEST_TIMEOUT_SECONDS = 300
CODE_PREFIXES = ("scripts/", "bf_price_monitor/", "tests/", ".github/")
PROGRESS_FILE = "docs/progress.md"

try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
if isinstance(payload, dict) and payload.get("stop_hook_active"):
    sys.exit(0)  # already continuing because of this hook -- let Claude stop
cwd = (payload.get("cwd") if isinstance(payload, dict) else None) or "."


def git(*args):
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


commit_time = git("log", "-1", "--format=%ct")
if commit_time is None or not commit_time.isdigit():
    sys.exit(0)  # not a git repo / no commits: nothing to verify

recent_commit = (time.time() - int(commit_time)) < RECENT_COMMIT_MINUTES * 60
dirty_python = [
    f
    for f in (git("diff", "--name-only", "HEAD") or "").splitlines()
    if f.endswith(".py")
]
if not recent_commit and not dirty_python:
    sys.exit(0)

problems = []

if recent_commit:
    committed = (git("log", "-1", "--name-only", "--format=") or "").splitlines()
    touched_code = any(f.startswith(CODE_PREFIXES) for f in committed)
    if touched_code and PROGRESS_FILE not in committed:
        problems.append(
            f"last commit changed code but not {PROGRESS_FILE} -- append the 2-line "
            "handoff summary (task, tests passing, commit SHA) and commit it."
        )

if recent_commit or dirty_python:
    try:
        tests = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "--tb=no", "-q"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
        if tests.returncode != 0:
            tail = "\n".join(tests.stdout.strip().splitlines()[-15:])
            problems.append(f"pytest exited {tests.returncode}:\n{tail}")
    except subprocess.TimeoutExpired:
        print(
            f"[verify_completion] pytest exceeded {PYTEST_TIMEOUT_SECONDS}s; result unknown, not blocking.",
            file=sys.stderr,
        )
    except OSError as exc:
        print(
            f"[verify_completion] could not run pytest ({exc}); not blocking.",
            file=sys.stderr,
        )

if problems:
    print(
        "[verify_completion] unverified work:\n- " + "\n- ".join(problems),
        file=sys.stderr,
    )
    sys.exit(2)
sys.exit(0)
