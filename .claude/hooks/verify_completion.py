#!/usr/bin/env python3
"""
Stop hook: verify_completion.py (bf-price-monitor project-scoped)
Self-verification when Claude finishes a reply. Two modes, one Stop entry each
in .claude/settings.json:

  progress  (synchronous, fast) blocks with exit 2 when the latest commit
            touched code (scripts/, bf_price_monitor/, tests/, .github/) but
            not docs/progress.md -- the handoff log CLAUDE.md requires.
  tests     (asyncRewake, background) runs pytest and exits 2 on a failure,
            which wakes Claude with the output tail. The reply is not held up
            for the ~5 minute suite. A green result is cached by tree state,
            so an unchanged tree is never re-tested; a second failure on the
            same tree exits 0 so Claude is not woken in a loop.

Stop fires after EVERY reply, so both modes only act when there is recent
work: HEAD was committed within RECENT_COMMIT_MINUTES, or tracked Python
files have uncommitted changes. Otherwise (Q&A, read-only turns) they exit 0.
Exit 1 would be a non-blocking error in Claude Code, so failures use exit 2.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RECENT_COMMIT_MINUTES = 20
PYTEST_TIMEOUT_SECONDS = 600
LOCK_STALE_SECONDS = PYTEST_TIMEOUT_SECONDS + 60
CODE_PREFIXES = ("scripts/", "bf_price_monitor/", "tests/", ".github/")
PROGRESS_FILE = "docs/progress.md"
STATE_DIR = Path(".claude") / "logs"
CACHE_NAME = "verify_cache.json"
LOCK_NAME = "verify.lock"
TAIL_LINES = 15


def git(cwd, *args):
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def recent_work(cwd):
    """Return (recent_commit, dirty_python), or None when not a git repo."""
    commit_time = git(cwd, "log", "-1", "--format=%ct")
    if commit_time is None or not commit_time.isdigit():
        return None
    recent_commit = (time.time() - int(commit_time)) < RECENT_COMMIT_MINUTES * 60
    dirty_python = [
        f
        for f in (git(cwd, "diff", "--name-only", "HEAD") or "").splitlines()
        if f.endswith(".py")
    ]
    return recent_commit, dirty_python


def check_progress(cwd):
    """Problems with the latest commit's handoff log (empty list = fine)."""
    work = recent_work(cwd)
    if work is None or not work[0]:
        return []
    committed = (git(cwd, "log", "-1", "--name-only", "--format=") or "").splitlines()
    touched_code = any(f.startswith(CODE_PREFIXES) for f in committed)
    if touched_code and PROGRESS_FILE not in committed:
        return [
            f"last commit changed code but not {PROGRESS_FILE} -- append the 2-line "
            "handoff summary (task, tests passing, commit SHA) and commit it."
        ]
    return []


def tree_key(cwd):
    """Fingerprint of everything pytest could see: HEAD, tracked diff and
    untracked Python files (name and size)."""
    digest = hashlib.sha256()
    digest.update((git(cwd, "rev-parse", "HEAD") or "").encode())
    digest.update((git(cwd, "diff", "HEAD") or "").encode())
    untracked = (
        git(cwd, "ls-files", "--others", "--exclude-standard") or ""
    ).splitlines()
    for name in sorted(f for f in untracked if f.endswith(".py")):
        try:
            size = (Path(cwd) / name).stat().st_size
        except OSError:
            size = -1
        digest.update(f"{name}:{size}".encode())
    return digest.hexdigest()


def read_cache(state_dir):
    try:
        data = json.loads((state_dir / CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(state_dir, data):
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / CACHE_NAME).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # a missing cache only costs one extra run


def take_lock(state_dir):
    """True when this process now holds the lock. A lock older than
    LOCK_STALE_SECONDS is a crashed run and is taken over."""
    lock = state_dir / LOCK_NAME
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
            lock.unlink()
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        return False
    return True


def release_lock(state_dir):
    try:
        (state_dir / LOCK_NAME).unlink()
    except OSError:
        pass


def pytest_python(cwd):
    venv_python = Path(cwd) / ".venv" / "Scripts" / "python.exe"
    return str(venv_python) if venv_python.is_file() else sys.executable


def run_pytest(cwd):
    """Return (returncode, output_tail); returncode None means the run could
    not finish (timeout or could not start) and the result is unknown."""
    try:
        tests = subprocess.run(
            [pytest_python(cwd), "-m", "pytest", "-x", "--tb=line", "-q"],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # keep crashes (e.g. PermissionError) visible
            text=True,
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, f"pytest exceeded {PYTEST_TIMEOUT_SECONDS}s; result unknown"
    except OSError as exc:
        return None, f"could not run pytest ({exc})"
    tail = "\n".join(tests.stdout.strip().splitlines()[-TAIL_LINES:])
    return tests.returncode, tail


def check_tests(cwd, runner=run_pytest):
    """Run the suite when the tree changed. Returns (exit_code, message)."""
    work = recent_work(cwd)
    if work is None or not (work[0] or work[1]):
        return 0, ""
    state_dir = Path(cwd) / STATE_DIR
    key = tree_key(cwd)
    cache = read_cache(state_dir)
    if cache.get("green") == key:
        return 0, ""  # this exact tree already passed
    if not take_lock(state_dir):
        return 0, "[verify_completion] another test run is in progress; skipping."
    try:
        code, tail = runner(cwd)
        if code is None:
            return 0, f"[verify_completion] {tail}; not blocking."
        if code == 0:
            write_cache(state_dir, {"green": key})
            return 0, ""
        if cache.get("failed") == key:
            return 0, (
                "[verify_completion] pytest still fails on the same tree "
                f"(exit {code}); not waking Claude again."
            )
        write_cache(state_dir, {**cache, "failed": key})
        return 2, f"[verify_completion] pytest exited {code}:\n{tail}"
    finally:
        release_lock(state_dir)


def main(argv):
    mode = argv[1] if len(argv) > 1 else "progress"
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    cwd = payload.get("cwd") or "."
    if mode == "progress":
        if payload.get("stop_hook_active"):
            return 0  # already continuing because of this hook -- let Claude stop
        problems = check_progress(cwd)
        if problems:
            print(
                "[verify_completion] unverified work:\n- " + "\n- ".join(problems),
                file=sys.stderr,
            )
            return 2
        return 0
    if mode == "tests":
        code, message = check_tests(cwd)
        if message:
            print(message, file=sys.stderr)
        return code
    print(f"[verify_completion] unknown mode {mode!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
