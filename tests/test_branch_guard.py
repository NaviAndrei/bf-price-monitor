"""Tests for .claude/hooks/branch_guard.py (tokenising PreToolUse guard)."""

import importlib.util
import json
import shlex
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
    ("git merge --no-edit origin/master", "master", "merge allowed on master"),
    ("git branch -D old", "feature/x", "delete only blocked on protected branches"),
    ("git rebase main", "feature/x", "rebase only blocked on protected branches"),
    ("git push", "master", "plain push"),
    ("git push -u origin master", "master", "-u is not a force flag"),
    ("git push origin master --follow-tags", "master", "--follow-tags is not --force"),
    ("git push --tags", "master", "tags push"),
    ("git push --dry-run origin master", "master", "dry run"),
    ('echo "git push --force"', "master", "quoted text is not a git call"),
    ("git log --oneline", "master", "read-only"),
    ("git commit -m 'document the -f flag'", "master", "message text is not a flag"),
    ("git worktree list", "master", "listing worktrees creates nothing"),
    ("git checkout master", "master", "checkout of an existing branch"),
    ("git switch master", "master", "switch to an existing branch"),
    ("git checkout -- tests/test_branch_guard.py", "master", "file restore"),
    ("git update-ref refs/tags/v1 HEAD", "master", "only refs/heads is a branch"),
    ("cmd /c git status", "master", "read-only inside cmd /c"),
    (
        'powershell -Command "git log --oneline"',
        "master",
        "read-only inside powershell",
    ),
    ('bash -c "echo git push --force"', "master", "text inside echo, not a git call"),
    (r"C:\Git\cmd\git.exe status", "master", "unquoted Windows git path, read-only"),
    (r'"C:\Program Files\Git\cmd\git.exe" log', "master", "quoted Windows git path"),
    ("git fetch --force origin", "master", "fetch -f is not a push"),
    ("git push origin master:master", "master", "explicit same-name refspec"),
    ("git push origin HEAD:master", "master", "refspec with a source is not a delete"),
    ('powershell -Command "& { git status }"', "master", "read-only script block"),
    ("& git log --oneline", "master", "call operator, read-only"),
    ("{ git status; }", "master", "bash group, read-only"),
    ("git log --format={%h}", "master", "braces inside an argument"),
    (
        "pwsh -NoProfile -ExecutionPolicy Bypass -File run.ps1",
        "master",
        "-ExecutionPolicy is not -EncodedCommand",
    ),
    ('powershell -Command "echo -e"', "master", "-e inside the command text"),
    ("powershell -c echo -e", "master", "flags after -c belong to the command"),
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
    ("git branch -d old", "master", "lowercase delete on protected branch"),
    ("git branch -m old new", "master", "rename on protected branch"),
    ("git branch -M new", "master", "forced rename on protected branch"),
    ("git status && git checkout -b x", "master", "creation in a later segment"),
    ("git status\ngit checkout -b x", "master", "creation after a newline"),
    ("git -C . checkout -b x", "master", "global option must not hide the subcommand"),
    ('bash -c "git checkout -b x"', "master", "creation inside a shell -c string"),
    ("git commit -m x", "master-fkamih", "master-fkamih is no longer allowed"),
    # force push, any token order
    ("git push --force origin master", "master", "force push"),
    ("git push origin master --force", "master", "flag after arguments"),
    ("git push origin +master", "master", "plus refspec forces"),
    ("git push --force-with-lease origin master", "master", "lease still forces"),
    ("git push --force-with-lease=master:abc", "master", "lease with value"),
    ("git push --force-if-includes", "master", "force-if-includes"),
    ("git push -f", "master", "short flag"),
    ("git push -fu origin master", "master", "f inside combined short flags"),
    ("git -C . push origin master -f", "master", "global option, trailing flag"),
    ("git push -f origin x", "feature/x", "blocked outside allowed branches too"),
    # more branch-creating forms
    ("git worktree add ../wt", "master", "bare worktree add makes a branch"),
    ("git worktree add ../wt master", "master", "worktree add with commit-ish"),
    ("git checkout --orphan x", "master", "orphan branch"),
    ("git switch --orphan x", "master", "orphan branch"),
    ("git checkout --track origin/foo", "master", "tracking branch"),
    ("git checkout -t origin/foo", "master", "tracking branch, short"),
    ("git switch --track origin/foo", "master", "tracking branch"),
    ("git switch -t origin/foo", "master", "tracking branch, short"),
    ("git update-ref refs/heads/x HEAD", "master", "branch via update-ref"),
    # wrappers
    ("cmd /c git branch newname", "master", "cmd /c"),
    ('cmd.exe /c "git checkout -b x"', "master", "cmd.exe /c with one string"),
    ('powershell -Command "git push --force"', "master", "powershell -Command"),
    ('pwsh -NoProfile -c "git worktree add ../x"', "master", "pwsh -c after flags"),
    ('sh -c "git switch -c x"', "master", "sh -c"),
    (r'bash -c "cmd /c \"git branch x\""', "master", "bash wrapping cmd"),
    # program-name normalisation
    (r"C:\Git\cmd\git.exe branch newname", "master", "unquoted Windows path"),
    (r'"C:\Program Files\Git\cmd\git.exe" checkout -b x', "master", "quoted path"),
    (r"C:\Git\cmd\git.exe push --force", "master", "force via Windows path"),
    (r"C:\Windows\System32\cmd.exe /c git branch x", "master", "cmd.exe by path"),
    ("GIT.EXE branch newname", "master", "case and .exe are ignored"),
    # remote branch delete
    ("git push origin :old", "master", "empty source deletes the remote branch"),
    ("git push origin :refs/heads/old", "master", "full ref delete"),
    ("git -C . push origin master :old", "master", "delete after other arguments"),
    # PowerShell script blocks and the call operator
    ('powershell -Command "& { git branch x }"', "master", "script block"),
    ('pwsh -c "& { git push --force }"', "master", "script block, force push"),
    ('pwsh -c "& { & { git branch x } }"', "master", "nested script blocks"),
    ("& git branch x", "master", "call operator"),
    (r'& "C:\Git\cmd\git.exe" checkout -b x', "master", "call operator with path"),
    ("{ git branch x; }", "master", "bash group"),
    # encoded commands cannot be inspected
    ("powershell -EncodedCommand ZwBpAHQA", "master", "full flag"),
    ("pwsh -enc ZwBpAHQA", "master", "-enc"),
    ("PWSH -EC ZwBpAHQA", "master", "-ec in any case"),
    ("powershell -NoProfile -e ZwBpAHQA", "master", "-e after other flags"),
    ("cmd /c powershell -enc ZwBpAHQA", "master", "encoded command inside cmd"),
]


def _wrap(command, times, shell="bash -c"):
    for _ in range(times):
        command = f"{shell} {shlex.quote(command)}"
    return command


NESTED = [
    (_wrap("git branch x", 3), "master", "three wrapper levels are still unwrapped"),
    (_wrap("git push -f", 2, "cmd /c"), "master", "two cmd levels"),
    (_wrap("git status", 4), "master", "more than three levels is refused"),
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


@pytest.mark.parametrize(
    ("command", "branch", "why"),
    NESTED,
    ids=[f"{b}:nested{i}" for i, (_, b, _) in enumerate(NESTED)],
)
def test_nested_wrappers(command, branch, why):
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
