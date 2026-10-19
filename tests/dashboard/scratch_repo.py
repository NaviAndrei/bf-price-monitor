"""Scratch-repository helpers for the slice C server tests.

A throwaway bare origin plus two clones in a temp directory: ``dash`` (the
dashboard's dedicated clone) and ``other`` (a second writer). No network, no
real clone, nothing outside ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bf_price_monitor.dashboard import config, ids

WATCHLIST = Path("data") / "watchlist.json"
AUTHOR = ("Dash Author", "dash@example.com")


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout.strip()


def make_watch(site: str, query: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": ids.watch_id(site, query),
        "owner": "NaviAndrei",
        "site": site,
        "query": query,
        "track_all_time_low": True,
        "seller_policy": "any",
        "cadence_minutes": 120,
        "enabled": True,
        **extra,
    }


def seed_doc() -> dict[str, Any]:
    return {
        "watches": [
            make_watch(
                "emag", "laptop lenovo v15", target_price=2500.0, channels=["telegram"]
            ),
            make_watch("pcgarage", "laptop asus vivobook", min_drop_percent=5.0),
        ]
    }


class Scratch:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.clone = root / "dash"
        self.other = root / "other"

    @property
    def cfg(self) -> config.DashboardConfig:
        return config.DashboardConfig(self.clone, *AUTHOR)

    def origin_doc(self) -> dict[str, Any]:
        raw = git(self.origin, "show", f"master:{WATCHLIST.as_posix()}")
        doc: dict[str, Any] = json.loads(raw)
        return doc

    def origin_subjects(self) -> list[str]:
        return git(self.origin, "log", "--format=%s", "master").splitlines()

    def clone_head_attached(self) -> bool:
        done = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"], cwd=self.clone, capture_output=True
        )
        return done.returncode == 0

    def take_origin_offline(self) -> None:
        self.origin.rename(self.root / "origin.off")

    def bring_origin_back(self) -> None:
        (self.root / "origin.off").rename(self.origin)


def build_scratch(tmp_path: Path) -> Scratch:
    """A bare origin with a seeded master plus a `dash` and an `other` clone."""
    s = Scratch(tmp_path)
    git(tmp_path, "init", "-q", "--bare", str(s.origin))
    git(s.origin, "symbolic-ref", "HEAD", "refs/heads/master")
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", str(seed))
    (seed / WATCHLIST).parent.mkdir()
    (seed / WATCHLIST).write_text(
        json.dumps(seed_doc(), indent=2) + "\n", encoding="utf-8"
    )
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "base")
    git(seed, "push", "-q", str(s.origin), "HEAD:master")
    git(tmp_path, "clone", "-q", str(s.origin), "dash")
    git(tmp_path, "clone", "-q", str(s.origin), "other")
    return s


def other_edit_watchlist(s: Scratch, mutate: Callable[[dict[str, Any]], None]) -> None:
    """A second writer lands a watchlist edit on origin."""
    git(s.other, "pull", "-q", "--no-rebase", "origin", "master")
    path = s.other / WATCHLIST
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    git(s.other, "commit", "-q", "-am", "other edit")
    git(s.other, "push", "-q", "origin", "HEAD:master")
