"""Tests for .claude/hooks/branch_guard.py (tokenising PreToolUse guard)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "branch_guard.py"
)
_spec = importlib.util.spec_from_file_location("branch_guard", HOOK_PATH)
branch_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(branch_guard)

# (command, current branch, why)
ALLOWED = [
    ("git branch -vv", "master", "read-only listing, the original -vv bug"),
    ("git branch -v", "master", "read-only listing"),
    ("git branch -a", "master", "read-only listing"),
    ("git branch -r", "master", "read-only listing"),
    ("git branch", "master", "no args lists branches"),
    ("git branch --list 'feat*'", "master", "pattern with --list is a listing"),
    ("git branch --show-current", "master", "read-only"),
    ("git branch --contains abc123", "master", "listing filter, not creation"),
    ("git branch --sort -committerdate", "master", "sort key is a value, not a name"),
    ("git branch -u origin/master", "master", "sets upstream, creates nothing"),
    ("git status -sb", "master", "read-only"),
    ("git log --oneline --graph -n 8", "master", "read-only"),
    ("git diff --stat origin/master...master", "master", "read-only"),
    ("git show HEAD", "master", "read-only"),
    ("git fetch origin", "master", "read-only"),
    ("git rev-parse --abbrev-ref HEAD", "master", "read-only"),
    ("git remote -v", "master", "read-only"),
    ("git stash list", "master", "read-only"),
    ('echo "git checkout -b x"', "master", "quoted text is not a git call"),
    (
        'git status && echo "a; git switch -c x"',
        "master",
        "quoted operator and command",
    ),
    ("git -C . status", "master", "global option before a read-only subcommand"),
    ("git commit -m 'msg'", "master", "commit allowed on master"),
    ("git push origin master", "master", "push allowed on master"),
    (
        "git merge --no-edit origin/master",
        "master-fkamih",
        "merge allowed on master-fkamih",
    ),
    ("git branch -D old", "feature/x", "delete only blocked on protected branches"),
    ("git rebase main", "feature/x", "rebase only blocked on protected branches"),
]

BLOCKED = [
    ("git branch newbranch", "master", "branch creation"),
    ("git checkout -b x", "master", "branch creation"),
    ("git checkout -B x", "feature/x", "forced branch creation, blocked everywhere"),
    ("git switch -c x", "master", "branch creation"),
    ("git switch -C x", "feature/y", "forced branch creation, blocked everywhere"),
    ("git branch -c old new", "master", "branch copy creates a branch"),
    ("git worktree add -b x ../x", "master", "worktree with new branch"),
    ("git commit -m x", "feature/x", "commit outside allowed branches"),
    ("git push origin x", "feature/x", "push outside allowed branches"),
    ("git merge main", "feature/x", "merge outside allowed branches"),
    ("git reset --hard HEAD~1", "master", "hard reset on protected branch"),
    ("git rebase origin/master", "master", "rebase on protected branch"),
    ("git branch -D old", "master", "delete on protected branch"),
    ("git branch -d old", "master-fkamih", "delete on protected branch"),
    ("git branch -m old new", "master", "rename on protected branch"),
    ("git branch -M new", "master", "forced rename on protected branch"),
    ("git status && git checkout -b x", "master", "creation in a later segment"),
    ("git status\ngit checkout -b x", "master", "creation after a newline"),
    ("git -C . checkout -b x", "master", "global option must not hide the subcommand"),
    ('bash -c "git checkout -b x"', "master", "creation inside a shell -c string"),
]


def _evaluate(command, branch):
    return branch_guard.evaluate(command, lambda: branch)


@pytest.mark.parametrize(
    ("command", "branch", "why"),
    ALLOWED,
    ids=[f"{b}:{c!r}" for c, b, _ in ALLOWED],
)
def test_allowed(command, branch, why):
    assert _evaluate(command, branch) is None, why


@pytest.mark.parametrize(
    ("command", "branch", "why"),
    BLOCKED,
    ids=[f"{b}:{c!r}" for c, b, _ in BLOCKED],
)
def test_blocked(command, branch, why):
    reason = _evaluate(command, branch)
    assert reason is not None and reason.startswith("BLOCKED"), why


def test_unparsable_command_fails_open_with_warning(capsys):
    assert _evaluate('echo "unterminated', "master") is None
    assert "could not parse" in capsys.readouterr().err


def _run_hook(command, tool_name="Bash"):
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_block_exits_2_with_reason_on_stderr_only():
    result = _run_hook("git checkout -b x")
    assert result.returncode == 2
    assert "[branch_guard] BLOCKED" in result.stderr
    assert result.stdout == ""


def test_read_only_command_exits_0():
    result = _run_hook("git branch -vv")
    assert result.returncode == 0
    assert result.stderr == ""


def test_non_shell_tool_is_ignored():
    assert _run_hook("git checkout -b x", tool_name="Edit").returncode == 0
