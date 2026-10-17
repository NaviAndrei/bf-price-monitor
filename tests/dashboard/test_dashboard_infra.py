"""Slice B: journal, OS/thread locking, git command wrapper, clone config."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from bf_price_monitor.dashboard import config, gitcmd, journal, locking, operations

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_SRC = REPO_ROOT / "bf_price_monitor" / "dashboard"


def _op(query: str = "a") -> dict[str, Any]:
    return operations.make_add("emag", query)


# --- journal ---------------------------------------------------------------


def test_journal_appends_in_order_and_survives_a_new_instance(tmp_path: Path):
    j = journal.Journal(tmp_path / "bf")
    first = j.append(_op("one"))
    second = j.append(_op("two"))
    assert first["op_id"] != second["op_id"]
    reopened = journal.Journal(tmp_path / "bf")
    assert [e["op"]["watch"]["query"] for e in reopened.pending()] == ["one", "two"]


def test_journal_remove_rewrites_atomically_and_leaves_no_temp_files(tmp_path: Path):
    j = journal.Journal(tmp_path / "bf")
    keep, drop = j.append(_op("keep")), j.append(_op("drop"))
    j.remove([drop["op_id"]])
    assert [e["op_id"] for e in j.pending()] == [keep["op_id"]]
    assert sorted(p.name for p in (tmp_path / "bf").iterdir()) == ["pending.jsonl"]


def test_journal_move_to_conflicts_keeps_the_op_and_the_reason(tmp_path: Path):
    j = journal.Journal(tmp_path / "bf")
    entry = j.append(_op("x"))
    j.move_to_conflicts(entry, reason="watch changed", kind="conflict")
    assert j.pending() == []
    [conflict] = j.conflicts()
    assert conflict["op_id"] == entry["op_id"]
    assert conflict["reason"] == "watch changed"
    assert conflict["kind"] == "conflict"
    assert conflict["op"] == entry["op"]


def test_journal_dismiss_conflict(tmp_path: Path):
    j = journal.Journal(tmp_path / "bf")
    entry = j.append(_op())
    j.move_to_conflicts(entry, reason="r", kind="conflict")
    assert j.dismiss_conflict(entry["op_id"]) is True
    assert j.dismiss_conflict(entry["op_id"]) is False
    assert j.conflicts() == []


def test_journal_corrupt_line_raises_instead_of_dropping_edits(tmp_path: Path):
    j = journal.Journal(tmp_path / "bf")
    j.append(_op())
    with (tmp_path / "bf" / "pending.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{torn write\n")
    with pytest.raises(journal.JournalError):
        j.pending()


def test_journal_empty_when_nothing_was_written(tmp_path: Path):
    j = journal.Journal(tmp_path / "does-not-exist-yet")
    assert j.pending() == [] and j.conflicts() == []


# --- locking ---------------------------------------------------------------

_HOLDER = """
import sys, time
from pathlib import Path
from bf_price_monitor.dashboard.locking import CloneLock
with CloneLock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.readline()
"""


def test_concurrent_holders_in_one_process_are_serialized(tmp_path: Path):
    lock_path = tmp_path / "lock"
    inside = 0
    peak = 0
    guard = threading.Lock()

    def work() -> None:
        nonlocal inside, peak
        with locking.CloneLock(lock_path):
            with guard:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.05)
            with guard:
                inside -= 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1


def test_second_process_lock_fails_fast(tmp_path: Path):
    lock_path = tmp_path / "bf" / "lock"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with pytest.raises(locking.CloneBusy):
            with locking.CloneLock(lock_path):
                pytest.fail("acquired a lock another process holds")
        assert time.monotonic() - started < 2
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=10)
    with locking.CloneLock(lock_path):  # released once the holder exits
        pass


# --- gitcmd ----------------------------------------------------------------


def test_git_env_disables_prompts():
    seen: dict[str, Any] = {}

    def runner(args, cwd, env, timeout):
        seen.update(env=env, timeout=timeout)
        return subprocess.CompletedProcess(args, 0, "", "")

    gitcmd.Git(Path("."), runner=runner).run("status")
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GCM_INTERACTIVE"] == "never"


def test_default_runner_passes_env_and_timeout_to_subprocess(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(gitcmd.subprocess, "run", fake_run)
    env = {**os.environ, **gitcmd.GIT_ENV}
    gitcmd.default_runner(["status"], tmp_path, env, 7.0)
    assert seen["cmd"] == ["git", "status"]
    assert seen["timeout"] == 7.0
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["cwd"] == tmp_path


def test_timeouts_are_30s_for_network_and_10s_for_local():
    assert gitcmd.NETWORK_TIMEOUT == 30
    assert gitcmd.LOCAL_TIMEOUT == 10
    timeouts: list[float] = []

    def runner(args, cwd, env, timeout):
        timeouts.append(timeout)
        return subprocess.CompletedProcess(args, 0, "", "")

    g = gitcmd.Git(Path("."), runner=runner)
    g.run("status")
    g.run("fetch", timeout=gitcmd.NETWORK_TIMEOUT)
    assert timeouts == [10, 30]


def test_subprocess_timeout_becomes_git_timeout(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(gitcmd.subprocess, "run", fake_run)
    with pytest.raises(gitcmd.GitTimeout):
        gitcmd.default_runner(["push"], tmp_path, dict(os.environ), 1.0)


def test_git_run_raises_giterror_with_stderr_on_failure():
    def runner(args, cwd, env, timeout):
        return subprocess.CompletedProcess(args, 128, "", "fatal: nope\n")

    with pytest.raises(gitcmd.GitError) as exc:
        gitcmd.Git(Path("."), runner=runner).run("fetch")
    assert exc.value.returncode == 128 and "nope" in exc.value.stderr


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("fatal: Authentication failed for 'https://github.com/x/y.git/'", "auth"),
        (
            "fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled",
            "auth",
        ),
        (
            "remote: Permission to x/y.git denied to z.\nfatal: unable to access: 403",
            "auth",
        ),
        (" ! [rejected]        HEAD -> master (fetch first)", "rejected"),
        (" ! [rejected]        HEAD -> master (non-fast-forward)", "rejected"),
        (
            "fatal: unable to access 'https://x': Could not resolve host: github.com",
            "network",
        ),
        (
            "fatal: '/gone' does not appear to be a git repository\n"
            "fatal: Could not read from remote repository.",
            "network",
        ),
        ("something entirely different", "other"),
    ],
)
def test_push_failure_classification(stderr: str, reason: str):
    assert gitcmd.classify_failure(stderr) == reason


# --- forbidden verbs -------------------------------------------------------

FORBIDDEN_GIT = [
    r"reset\s+--hard",
    r"\brebase\b",
    r"--force\b",
    r"push\s+-f\b",
    r"checkout\s+-b",
    r"switch\s+-c",
    r"\+refs/",
]
FORBIDDEN_ARGS = r"""["'](rebase|reset|clean|--force|--force-with-lease|-f|-B|-b)["']"""


def test_sync_sources_have_no_forbidden_git_verbs():
    sources = sorted(DASHBOARD_SRC.glob("*.py"))
    assert sources, "dashboard package not found"
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_GIT:
            assert not re.search(pattern, text), f"{path.name}: {pattern}"
        assert not re.search(FORBIDDEN_ARGS, text), f"{path.name}: forbidden git arg"


def test_package_import_does_not_pull_in_the_web_server():
    code = (
        "import sys, bf_price_monitor.dashboard as d;"
        "bad = [m for m in ('fastapi', 'uvicorn', 'starlette') if m in sys.modules];"
        "assert not bad, bad"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr


# --- config ----------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", str(seed))
    (seed / "f.txt").write_text("x", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "push", "-q", str(origin), "HEAD:master")
    _git(tmp_path, "clone", "-q", str(origin), "clone")
    return tmp_path / "clone"


def test_config_requires_the_clone_variable():
    with pytest.raises(config.ConfigError, match="BF_DASHBOARD_CLONE"):
        config.load_config({})


def test_config_reads_clone_and_author_from_env(tmp_path: Path):
    cfg = config.load_config(
        {
            "BF_DASHBOARD_CLONE": str(tmp_path),
            "BF_DASHBOARD_GIT_NAME": "Someone",
            "BF_DASHBOARD_GIT_EMAIL": "s@example.com",
        }
    )
    assert cfg.clone == tmp_path
    assert (cfg.author_name, cfg.author_email) == ("Someone", "s@example.com")


def test_config_author_name_defaults_to_naviandrei(tmp_path: Path):
    cfg = config.load_config(
        {"BF_DASHBOARD_CLONE": str(tmp_path), "BF_DASHBOARD_GIT_EMAIL": "s@example.com"}
    )
    assert cfg.author_name == "NaviAndrei"


def test_validate_accepts_a_clean_dedicated_clone(clone: Path, tmp_path: Path):
    cfg = config.DashboardConfig(clone, "N", "n@example.com")
    config.validate_clone(cfg, dev_root=tmp_path / "somewhere-else")


def test_refuses_dev_checkout_and_dirty_clone(clone: Path, tmp_path: Path):
    cfg = config.DashboardConfig(clone, "N", "n@example.com")
    with pytest.raises(config.ConfigError, match="development checkout"):
        config.validate_clone(cfg, dev_root=clone)
    (clone / "f.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="uncommitted"):
        config.validate_clone(cfg, dev_root=tmp_path / "somewhere-else")


def test_validate_refuses_a_missing_origin_and_a_non_repository(
    clone: Path, tmp_path: Path
):
    _git(clone, "remote", "remove", "origin")
    cfg = config.DashboardConfig(clone, "N", "n@example.com")
    with pytest.raises(config.ConfigError, match="origin"):
        config.validate_clone(cfg, dev_root=tmp_path / "x")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(config.ConfigError, match="git repository"):
        config.validate_clone(
            config.DashboardConfig(plain, "N", "n@example.com"), dev_root=tmp_path / "x"
        )
    with pytest.raises(config.ConfigError, match="does not exist"):
        config.validate_clone(
            config.DashboardConfig(tmp_path / "nope", "N", "n@example.com"),
            dev_root=tmp_path / "x",
        )
