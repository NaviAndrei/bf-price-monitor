#!/usr/bin/env python3
"""
SessionStart hook: session_start.py (bf-price-monitor project-scoped)
Prints one short block of project state (latest CI run, label count, open
issue count). SessionStart stdout is added to Claude's context, so the
output is kept to a few plain ASCII lines. Every probe is best-effort: a
missing `gh`, no network, or a broken feedback CLI degrades to
"unavailable" instead of failing the session.
Always exits 0 (SessionStart cannot block anyway).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
PROBE_TIMEOUT_SECONDS = 12


def run(cmd):
    """Return stdout of `cmd`, or None on any failure (missing binary, non-zero exit, timeout)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def latest_ci_run():
    out = run(["gh", "run", "list", "--limit", "1", "--json", "status,conclusion,databaseId"])
    try:
        run_info = json.loads(out)[0]
    except (TypeError, ValueError, IndexError):
        return "CI: latest run unavailable"
    outcome = run_info.get("conclusion") or "in progress"
    return f"CI: latest run {run_info.get('status')} / {outcome} (id {run_info.get('databaseId')})"


def feedback_summary():
    unavailable = "Feedback: feedback summary unavailable"
    # feedback.py opens (and would create) the SQLite store; don't create a stray one.
    if not (ROOT / "data" / "price_history.db").exists():
        return unavailable
    out = run([sys.executable, "scripts/feedback.py", "summary"])
    if out is None:  # non-zero exit covers ImportError from the module's imports
        return unavailable
    for line in out.splitlines():
        if "current labels" in line:
            return "Feedback: " + line.replace("[feedback] ", "").strip()
    return unavailable


def open_issue_count():
    # gh defaults to 30 results; ask for enough that the count is real.
    out = run(["gh", "issue", "list", "--state", "open", "--limit", "1000", "--json", "number"])
    try:
        return f"Issues: {len(json.loads(out))} open issues"
    except (TypeError, ValueError):
        return "Issues: open issue count unavailable"


try:
    sys.stdin.read()  # drain the JSON payload; nothing in it is needed
except OSError:
    pass

lines = ["bf-price-monitor state:", latest_ci_run(), feedback_summary(), open_issue_count()]
sys.stdout.write("\n".join(lines) + "\n")
sys.exit(0)
