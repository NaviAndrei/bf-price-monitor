"""Where the dashboard clone lives and who its commits are authored by."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from bf_price_monitor.dashboard.gitcmd import (
    Git,
    GitError,
    GitTimeout,
    Runner,
    default_runner,
)

DEFAULT_AUTHOR_NAME = "NaviAndrei"
DEV_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class DashboardConfig:
    clone: Path
    author_name: str
    author_email: str
    remote: str = "origin"
    branch: str = "master"


def load_config(
    env: Mapping[str, str], runner: Runner = default_runner
) -> DashboardConfig:
    raw = env.get("BF_DASHBOARD_CLONE", "").strip()
    if not raw:
        raise ConfigError(
            "BF_DASHBOARD_CLONE is not set: point it at a dedicated clone of "
            "the repository (never your development checkout)"
        )
    clone = Path(raw)
    name = env.get("BF_DASHBOARD_GIT_NAME", "").strip() or DEFAULT_AUTHOR_NAME
    email = env.get("BF_DASHBOARD_GIT_EMAIL", "").strip()
    if not email:
        try:
            done = Git(clone, runner).run("config", "user.email", check=False)
        except (GitError, GitTimeout, OSError):
            done = None
        email = done.stdout.strip() if done is not None else ""
    if not email:
        raise ConfigError(
            "no author email: set BF_DASHBOARD_GIT_EMAIL or configure "
            "user.email inside the dashboard clone"
        )
    return DashboardConfig(clone=clone, author_name=name, author_email=email)


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def validate_clone(
    cfg: DashboardConfig,
    *,
    dev_root: Path | None = None,
    runner: Runner = default_runner,
) -> None:
    """Refuse anything that is not a clean, dedicated clone with an origin."""
    clone = cfg.clone
    if not clone.is_dir():
        raise ConfigError(f"dashboard clone {clone} does not exist")
    git = Git(clone, runner)
    top = git.run("rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0 or not _same_path(Path(top.stdout.strip() or "."), clone):
        raise ConfigError(f"{clone} is not the top level of a git repository")
    if not (clone / ".git").is_dir():
        raise ConfigError(f"{clone}/.git must be a directory (no worktrees)")
    if _same_path(clone, dev_root if dev_root is not None else DEV_ROOT):
        raise ConfigError(
            f"{clone} is the development checkout; use a separate clone so "
            "dashboard commits never touch your working tree"
        )
    if git.run("remote", "get-url", cfg.remote, check=False).returncode != 0:
        raise ConfigError(f"{clone} has no '{cfg.remote}' remote")
    status = git.run("status", "--porcelain", "--untracked-files=no")
    if status.stdout.strip():
        raise ConfigError(
            f"{clone} has uncommitted changes; clean it before starting "
            "(for example: git restore --staged --worktree .)"
        )
