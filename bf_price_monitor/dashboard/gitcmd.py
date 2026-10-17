"""The only place the dashboard runs git.

Every call gets prompts disabled (``GIT_TERMINAL_PROMPT=0`` for terminal
prompts, ``GCM_INTERACTIVE=never`` so Git Credential Manager on Windows does
not open a sign-in window) and a timeout. A hung command becomes
``GitTimeout`` instead of freezing the server.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
LOCAL_TIMEOUT = 10.0
NETWORK_TIMEOUT = 30.0

Runner = Callable[
    [Sequence[str], Path, Mapping[str, str], float],
    "subprocess.CompletedProcess[str]",
]


class GitTimeout(Exception):
    pass


class GitError(Exception):
    def __init__(self, args_: Sequence[str], returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        last = stderr.strip().splitlines()[-1:] or ["no error output"]
        super().__init__(f"git {args_[0]} failed (exit {returncode}): {last[0]}")


def default_runner(
    args: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout(f"git {args[0]} timed out after {timeout:g}s") from exc


class Git:
    def __init__(self, cwd: Path, runner: Runner = default_runner) -> None:
        self._cwd = cwd
        self._runner = runner

    def run(
        self, *args: str, timeout: float = LOCAL_TIMEOUT, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, **GIT_ENV}
        done = self._runner(args, self._cwd, env, timeout)
        if check and done.returncode != 0:
            raise GitError(args, done.returncode, done.stderr or "")
        return done


_AUTH = re.compile(
    r"authentication failed|could not read (username|password)|"
    r"terminal prompts disabled|permission to .* denied|permission denied|"
    r"invalid username or password|access denied|\b40[13]\b",
    re.IGNORECASE,
)
_REJECTED = re.compile(
    r"\[(remote )?rejected\]|non-fast-forward|fetch first|stale info", re.IGNORECASE
)
_NETWORK = re.compile(
    r"could not resolve host|unable to access|could not read from remote|"
    r"does not appear to be a git repository|connection (timed out|refused|reset)|"
    r"network is unreachable|timed out|early eof|failed to connect",
    re.IGNORECASE,
)


def classify_failure(stderr: str) -> str:
    """Why a fetch or push failed: auth, rejected, network or other."""
    if _AUTH.search(stderr):
        return "auth"
    if _REJECTED.search(stderr):
        return "rejected"
    if _NETWORK.search(stderr):
        return "network"
    return "other"
