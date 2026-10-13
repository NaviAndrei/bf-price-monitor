#!/usr/bin/env python3
"""
PreToolUse hook: branch_guard.py (bf-price-monitor project-scoped)
Solo-dev policy for this repo: all work happens directly on `master`.

The command is tokenised with shlex (so quoted text such as an echoed
string is never mistaken for a git invocation), split into segments on
; && || | & ( ) and newlines, and each segment starting with `git` is
judged on its subcommand and flags:

  * creating a branch (git branch <name>, checkout -b/-B, switch -c/-C,
    branch -c/-C, worktree add -b/-B) is blocked everywhere;
  * commit / push / merge are blocked on any branch outside
    ALLOWED_BRANCHES;
  * on ALLOWED_BRANCHES, reset --hard, rebase and branch -d/-D/-m/-M are
    blocked;
  * everything else, including every read-only form (branch -v/-vv/-a/-r/
    --list/--show-current, status, log, diff, show, fetch, ...), is allowed.

Exit code 2 = BLOCK (reason on stderr), exit code 0 = ALLOW.
Fail-open: a command that shlex cannot parse is allowed with a warning.
Known gap: command/process substitution ($(...), backticks) is not parsed.
"""

import json
import re
import shlex
import subprocess
import sys

ALLOWED_BRANCHES = {"master", "master-fkamih"}

SEPARATOR_CHARS = set(";&|()\n")
WRAPPERS = {"command", "env", "nohup", "sudo", "time"}
SHELLS = {"bash", "sh", "zsh", "dash", "pwsh", "powershell"}
SHELL_COMMAND_FLAGS = {"-c", "-command"}

GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
BRANCH_LONG_WITH_VALUE = {
    "--sort",
    "--format",
    "--abbrev",
    "--points-at",
    "--contains",
    "--merged",
    "--no-merged",
}
BRANCH_LIST_LONG = {
    "--list",
    "--all",
    "--remotes",
    "--show-current",
    "--contains",
    "--merged",
    "--no-merged",
    "--points-at",
}
BRANCH_UPSTREAM_LONG = {"--set-upstream-to", "--unset-upstream", "--edit-description"}


def _allowed_list():
    return ", ".join(sorted(ALLOWED_BRANCHES))


def split_segments(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    segments, current = [], []
    for token in lexer:
        if all(ch in SEPARATOR_CHARS for ch in token):
            segments.append(current)
            current = []
        else:
            current.append(token)
    segments.append(current)
    return [s for s in segments if s]


def _program_name(token):
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _strip_prefixes(tokens):
    tokens = list(tokens)
    while tokens and (
        re.match(r"^\w+=", tokens[0]) or _program_name(tokens[0]) in WRAPPERS
    ):
        tokens.pop(0)
    return tokens


def _git_subcommand(rest):
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        i += 2 if rest[i] in GIT_OPTIONS_WITH_VALUE else 1
    if i >= len(rest):
        return None, []
    return rest[i], rest[i + 1 :]


def _has_short_flag(args, letters):
    for token in args:
        if token == "--":
            break
        if token.startswith("-") and not token.startswith("--"):
            if any(ch in letters for ch in token[1:]):
                return True
    return False


def _parse_branch_args(args):
    shorts, longs, positional = "", set(), []
    it = iter(args)
    for token in it:
        if token == "--":
            positional.extend(it)
            break
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            longs.add(name)
            if "=" not in token and name in BRANCH_LONG_WITH_VALUE:
                next(it, None)
        elif token.startswith("-") and len(token) > 1:
            shorts += token[1:]
        else:
            positional.append(token)
    return shorts, longs, positional


def _classify_branch(args):
    """Return "create", "modify" (delete/rename) or None (read-only)."""
    shorts, longs, positional = _parse_branch_args(args)
    if "u" in shorts or longs & BRANCH_UPSTREAM_LONG:
        return None
    if set(shorts) & set("dDmM") or longs & {"--delete", "--move"}:
        return "modify"
    if set(shorts) & set("cC") or "--copy" in longs:
        return "create"
    listing = set(shorts) & set("lar") or longs & BRANCH_LIST_LONG
    if positional and not listing:
        return "create"
    return None


def _creates_branch(sub, args):
    if sub == "checkout":
        return _has_short_flag(args, "bB")
    if sub == "switch":
        return _has_short_flag(args, "cC") or bool(
            {"--create", "--force-create"} & set(args)
        )
    if sub == "worktree":
        return args[:1] == ["add"] and _has_short_flag(args[1:], "bB")
    if sub == "branch":
        return _classify_branch(args) == "create"
    return False


def _check_segment(segment, get_branch):
    tokens = _strip_prefixes(segment)
    if not tokens:
        return None
    program = _program_name(tokens[0])
    if program in SHELLS:
        for i, token in enumerate(tokens[1:], 1):
            if token.lower() in SHELL_COMMAND_FLAGS and i + 1 < len(tokens):
                return evaluate(tokens[i + 1], get_branch)
        return None
    if program != "git":
        return None

    sub, args = _git_subcommand(tokens[1:])
    if sub is None:
        return None

    if _creates_branch(sub, args):
        return (
            "BLOCKED: creating a new branch is disabled on this repo — all "
            f"work goes directly on an allowed branch ({_allowed_list()})."
        )

    if sub in ("commit", "push", "merge"):
        current = get_branch()
        if current and current not in ALLOWED_BRANCHES:
            allowed = ", ".join(f"'{b}'" for b in sorted(ALLOWED_BRANCHES))
            return (
                f"BLOCKED: currently on branch '{current}'. Commits, pushes and "
                f"merges are only allowed on: {allowed}. Switch to one of these first."
            )
        return None

    protected_action = None
    if sub == "reset" and "--hard" in args:
        protected_action = "git reset --hard"
    elif sub == "rebase":
        protected_action = "git rebase"
    elif sub == "branch" and _classify_branch(args) == "modify":
        protected_action = "git branch delete/rename"
    if protected_action and get_branch() in ALLOWED_BRANCHES:
        return (
            f"BLOCKED: '{protected_action}' is not allowed on protected branch "
            f"'{get_branch()}' ({_allowed_list()})."
        )
    return None


def evaluate(command, get_branch):
    """Return a block reason for `command`, or None when it is allowed."""
    try:
        segments = split_segments(command)
    except ValueError as exc:
        print(
            f"[branch_guard] could not parse command ({exc}); allowing it.",
            file=sys.stderr,
        )
        return None
    for segment in segments:
        reason = _check_segment(segment, get_branch)
        if reason:
            return f"{reason} Command: {command[:150]}"
    return None


def _current_branch(cwd):
    try:
        return subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def main():
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        return 0
    if input_data.get("tool_name", "") not in ("Bash", "PowerShell"):
        return 0

    command = input_data.get("tool_input", {}).get("command", "")
    cwd = input_data.get("cwd", "") or "."
    branch = []

    def get_branch():
        if not branch:
            branch.append(_current_branch(cwd))
        return branch[0]

    reason = evaluate(command, get_branch)
    if reason:
        print(f"[branch_guard] {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
