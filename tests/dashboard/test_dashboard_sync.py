"""Slice B: GitSync against throwaway bare origins. No network, no real origin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from bf_price_monitor.dashboard import config, gitcmd, ids, journal, locking, operations
from bf_price_monitor.dashboard.sync import GitSync

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST = Path("data") / "watchlist.json"
AUTHOR = ("Dash Author", "dash@example.com")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout.strip()


def _watch(site: str, query: str, **extra: Any) -> dict[str, Any]:
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


SEED = {
    "watches": [
        _watch("emag", "laptop lenovo v15", target_price=2500.0, channels=["telegram"]),
        _watch("pcgarage", "laptop asus vivobook", min_drop_percent=5.0),
    ]
}
WATCH_A = SEED["watches"][0]
WATCH_B = SEED["watches"][1]


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.clone = root / "dash"
        self.other = root / "other"
        self.sleeps: list[float] = []
        self.push_calls = 0

    def make_sync(self, **kw: Any) -> GitSync:
        cfg = config.DashboardConfig(self.clone, *AUTHOR)
        kw.setdefault("sleep_fn", self.sleeps.append)
        return GitSync(cfg, **kw)

    def origin_doc(self) -> dict[str, Any]:
        raw = _git(self.origin, "show", f"master:{WATCHLIST.as_posix()}")
        return json.loads(raw)

    def origin_subjects(self) -> list[str]:
        return _git(self.origin, "log", "--format=%s", "master").splitlines()

    def origin_commit_count(self) -> int:
        return len(self.origin_subjects())

    def competing_commit(self, filename: str) -> None:
        """A different writer lands a commit on origin (e.g. the bot)."""
        _git(self.other, "pull", "-q", "--no-rebase", "origin", "master")
        (self.other / filename).write_text(filename, encoding="utf-8")
        _git(self.other, "add", filename)
        _git(self.other, "commit", "-q", "-m", f"competing {filename}")
        _git(self.other, "push", "-q", "origin", "HEAD:master")

    def other_edit_watchlist(self, mutate) -> None:
        _git(self.other, "pull", "-q", "--no-rebase", "origin", "master")
        path = self.other / WATCHLIST
        doc = json.loads(path.read_text(encoding="utf-8"))
        mutate(doc)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        _git(self.other, "commit", "-q", "-am", "other edit")
        _git(self.other, "push", "-q", "origin", "HEAD:master")

    def take_origin_offline(self) -> None:
        self.origin.rename(self.root / "origin.off")

    def bring_origin_back(self) -> None:
        (self.root / "origin.off").rename(self.origin)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    _git(tmp_path, "init", "-q", "--bare", str(e.origin))
    _git(e.origin, "symbolic-ref", "HEAD", "refs/heads/master")
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", str(seed))
    (seed / WATCHLIST).parent.mkdir()
    (seed / WATCHLIST).write_text(json.dumps(SEED, indent=2) + "\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "push", "-q", str(e.origin), "HEAD:master")
    _git(tmp_path, "clone", "-q", str(e.origin), "dash")
    _git(tmp_path, "clone", "-q", str(e.origin), "other")
    return e


def _failing_push(stderr: str, times: int = 1):
    """Runner that makes the first `times` pushes fail, everything else real."""
    state = {"left": times}

    def runner(args, cwd, env, timeout):
        if args and args[0] == "push" and state["left"] > 0:
            state["left"] -= 1
            return subprocess.CompletedProcess(["git", *args], 1, "", stderr)
        return gitcmd.default_runner(args, cwd, env, timeout)

    return runner


# --- the happy path --------------------------------------------------------


def test_submit_pushes_one_commit_with_expected_message_and_author(env: Env):
    before = env.origin_commit_count()
    res = env.make_sync().submit(operations.make_add("emag", "ipad air"))
    assert res.state == "pushed" and res.pending_count == 0
    assert env.origin_commit_count() == before + 1
    assert env.origin_subjects()[0] == 'chore(watchlist): add emag "ipad air"'
    assert _git(env.origin, "log", "-1", "--format=%an|%ae", "master") == "|".join(
        AUTHOR
    )
    assert _git(env.origin, "log", "-1", "--format=%cn", "master") == AUTHOR[0]
    assert env.origin_doc()["watches"][-1]["query"] == "ipad air"


def test_submit_conflict_returns_conflict_and_changes_nothing(env: Env):
    stale = {**WATCH_A, "target_price": 1.0}
    op = operations.make_set_enabled(stale, False)
    before = env.origin_commit_count()
    sync = env.make_sync()
    res = sync.submit(op)
    assert res.state == "conflict"
    assert env.origin_commit_count() == before
    assert len(res.conflicts) == 1 and sync.status().pending_count == 0


def test_invalid_op_is_not_journaled_and_runs_no_git(env: Env):
    calls: list[Any] = []

    def runner(args, cwd, e, timeout):
        calls.append(args)
        return gitcmd.default_runner(args, cwd, e, timeout)

    sync = env.make_sync(runner=runner)
    res = sync.submit({"op": "add", "watch": {"site": "emag"}})
    assert res.state == "invalid"
    assert sync.status().pending_count == 0
    assert calls == []


def test_disable_then_enable_round_trips_to_the_same_file_content(env: Env):
    sync = env.make_sync()
    sync.submit(operations.make_set_enabled(WATCH_B, False))
    after_off = env.origin_doc()["watches"][1]
    assert after_off["enabled"] is False
    sync.submit(operations.make_set_enabled(after_off, True))
    assert env.origin_doc()["watches"][1] == WATCH_B


# --- rejected pushes -------------------------------------------------------


def test_rejected_push_retries_same_op_and_keeps_other_commit(env: Env):
    fired: list[int] = []

    def before_push() -> None:
        if not fired:
            fired.append(1)
            env.competing_commit("bot.txt")

    res = env.make_sync(before_push=before_push).submit(
        operations.make_add("flanco", "tv oled")
    )
    assert res.state == "pushed"
    assert env.sleeps == [1]
    subjects = env.origin_subjects()
    assert "competing bot.txt" in subjects
    assert 'chore(watchlist): add flanco "tv oled"' in subjects
    assert subjects.count('chore(watchlist): add flanco "tv oled"') == 1


def test_three_rejections_returns_rejected_and_keeps_op_pending(env: Env):
    counter = {"n": 0}

    def before_push() -> None:
        counter["n"] += 1
        if counter["n"] <= 3:  # the 4th push (the manual retry) is clear
            env.competing_commit(f"bot{counter['n']}.txt")

    sync = env.make_sync(before_push=before_push)
    res = sync.submit(operations.make_add("flanco", "tv oled"))
    assert res.state == "rejected"
    assert res.reason == "rejected"
    assert res.pending_count == 1
    assert env.sleeps == [1, 2]
    assert not any("tv oled" in s for s in env.origin_subjects())
    # A later retry lands it.
    assert sync.retry().state == "pushed"
    assert sync.status().pending_count == 0


def test_retry_ignores_stray_local_commit_in_clone(env: Env):
    (env.clone / "stray.txt").write_text("stray", encoding="utf-8")
    _git(env.clone, "add", "stray.txt")
    _git(env.clone, "commit", "-q", "-m", "stray local commit")
    res = env.make_sync().submit(operations.make_add("emag", "ipad air"))
    assert res.state == "pushed"
    assert "stray local commit" not in env.origin_subjects()
    # Nothing was rewritten: the stray commit is still on the clone's branch.
    assert "stray local commit" in _git(env.clone, "log", "--format=%s", "master")


# --- failed pushes and the journal -----------------------------------------


def test_failed_push_then_second_op_pushes_both(env: Env):
    """Origin unreachable for the first edit; once back, the second edit pushes
    both, in order, and the journal ends empty."""
    sync = env.make_sync()
    env.take_origin_offline()
    first = sync.submit(operations.make_add("emag", "first one"))
    assert first.state == "not_pushed" and first.reason == "network"
    assert first.pending_count == 1
    env.bring_origin_back()
    second = sync.submit(operations.make_add("emag", "second one"))
    assert second.state == "pushed" and second.pending_count == 0
    subjects = env.origin_subjects()
    assert subjects[:2] == [
        'chore(watchlist): add emag "second one"',
        'chore(watchlist): add emag "first one"',
    ]
    queries = [w["query"] for w in env.origin_doc()["watches"]]
    assert queries[-2:] == ["first one", "second one"]
    assert sync.status().pending_count == 0


def test_push_failure_after_a_local_commit_does_not_lose_it(env: Env):
    runner = _failing_push("fatal: unable to access 'x': Could not resolve host: h")
    sync = env.make_sync(runner=runner)
    first = sync.submit(operations.make_add("emag", "first one"))
    assert first.state == "not_pushed" and first.reason == "network"
    assert first.pending_count == 1
    second = sync.submit(operations.make_add("emag", "second one"))
    assert second.state == "pushed"
    assert env.origin_subjects()[:2] == [
        'chore(watchlist): add emag "second one"',
        'chore(watchlist): add emag "first one"',
    ]


def test_push_timeout_maps_to_not_pushed(env: Env):
    def runner(args, cwd, e, timeout):
        if args and args[0] == "push":
            raise gitcmd.GitTimeout("push timed out")
        return gitcmd.default_runner(args, cwd, e, timeout)

    sync = env.make_sync(runner=runner)
    res = sync.submit(operations.make_add("emag", "slow one"))
    assert res.state == "not_pushed"
    assert res.reason == "timeout"
    assert res.pending_count == 1
    assert env.sleeps == []


def test_fetch_timeout_maps_to_not_pushed(env: Env):
    def runner(args, cwd, e, timeout):
        if args and args[0] == "fetch":
            raise gitcmd.GitTimeout("fetch timed out")
        return gitcmd.default_runner(args, cwd, e, timeout)

    res = env.make_sync(runner=runner).submit(operations.make_add("emag", "x1"))
    assert (res.state, res.reason, res.pending_count) == ("not_pushed", "timeout", 1)


def test_auth_failure_is_reported_as_auth(env: Env):
    runner = _failing_push("fatal: Authentication failed for 'https://github.com/x/y'")
    res = env.make_sync(runner=runner).submit(operations.make_add("emag", "x1"))
    assert (res.state, res.reason) == ("not_pushed", "auth")
    assert res.pending_count == 1


def test_journal_survives_new_gitsync_instance(env: Env):
    env.take_origin_offline()
    env.make_sync().submit(operations.make_add("emag", "kept one"))
    env.bring_origin_back()
    restarted = env.make_sync()
    assert restarted.status().pending_count == 1
    assert restarted.retry().state == "pushed"
    assert env.origin_doc()["watches"][-1]["query"] == "kept one"


def test_retry_with_no_new_op_pushes_pending(env: Env):
    sync = env.make_sync()
    sync.journal.append(operations.make_add("emag", "queued one"))
    res = sync.retry()
    assert res.state == "pushed" and res.pending_count == 0
    assert env.origin_subjects()[0] == 'chore(watchlist): add emag "queued one"'


def test_retry_with_nothing_pending_is_a_cheap_success(env: Env):
    calls: list[Any] = []

    def runner(args, cwd, e, timeout):
        calls.append(args)
        return gitcmd.default_runner(args, cwd, e, timeout)

    res = env.make_sync(runner=runner).retry()
    assert res.state == "pushed" and res.pending_count == 0
    assert calls == []


def test_replayed_op_already_on_origin_is_dropped(env: Env):
    sync = env.make_sync()
    sync.journal.append(operations.make_set_enabled(WATCH_B, False))

    def disable_b(doc: dict[str, Any]) -> None:
        doc["watches"][1]["enabled"] = False

    env.other_edit_watchlist(disable_b)
    before = env.origin_commit_count()
    res = sync.retry()
    assert res.state == "pushed" and res.pending_count == 0
    assert env.origin_commit_count() == before
    assert sync.status().conflicts == []


def test_replayed_op_that_conflicts_moves_to_conflicts(env: Env):
    sync = env.make_sync()
    sync.journal.append(operations.make_update(WATCH_A, {"target_price": 100}))

    def change_a(doc: dict[str, Any]) -> None:
        doc["watches"][0]["target_price"] = 111.0

    env.other_edit_watchlist(change_a)
    before = env.origin_commit_count()
    res = sync.retry()
    assert res.state == "conflict"
    assert res.pending_count == 0
    [conflict] = sync.status().conflicts
    assert "changed" in conflict["reason"]
    assert env.origin_commit_count() == before
    assert env.origin_doc()["watches"][0]["target_price"] == 111.0


def test_conflict_cascades_later_ops_on_same_watch(env: Env):
    sync = env.make_sync()
    stale = operations.make_update(WATCH_A, {"target_price": 100})
    sync.journal.append(stale)

    def change_a(doc: dict[str, Any]) -> None:
        doc["watches"][0]["target_price"] = 111.0

    env.other_edit_watchlist(change_a)
    refreshed_a = env.origin_doc()["watches"][0]
    # Created after a refresh, so on its own it would apply cleanly.
    later = operations.make_update(refreshed_a, {"min_drop_percent": 7})
    unrelated = operations.make_set_enabled(WATCH_B, False)
    sync.journal.append(later)
    sync.journal.append(unrelated)

    res = sync.retry()
    conflicts = sync.status().conflicts
    assert [c["op"] for c in conflicts] == [stale, later]
    assert "earlier" in conflicts[1]["reason"]
    # The unrelated watch still went through; A is exactly what the other
    # writer left, with no min_drop_percent from the cascaded op.
    doc = env.origin_doc()
    assert doc["watches"][1]["enabled"] is False
    assert doc["watches"][0] == refreshed_a
    assert res.pending_count == 0


def test_submit_reports_its_own_fate_not_an_older_conflict(env: Env):
    sync = env.make_sync()
    sync.journal.append(operations.make_update(WATCH_A, {"target_price": 100}))

    def change_a(doc: dict[str, Any]) -> None:
        doc["watches"][0]["target_price"] = 111.0

    env.other_edit_watchlist(change_a)
    res = sync.submit(operations.make_set_enabled(WATCH_B, False))
    assert res.state == "pushed"
    assert len(res.conflicts) == 1


def test_dismiss_conflict_clears_it(env: Env):
    sync = env.make_sync()
    sync.journal.append(operations.make_update(WATCH_A, {"target_price": 100}))
    env.other_edit_watchlist(lambda d: d["watches"][0].update(target_price=5.0))
    sync.retry()
    [conflict] = sync.status().conflicts
    assert sync.dismiss_conflict(conflict["op_id"]) is True
    assert sync.status().conflicts == []


def test_origin_in_the_legacy_format_is_reported_invalid_and_keeps_the_op(env: Env):
    _git(env.other, "pull", "-q", "--no-rebase", "origin", "master")
    path = env.other / WATCHLIST
    path.write_text('[{"site": "emag", "query": "a"}]\n', encoding="utf-8")
    _git(env.other, "commit", "-q", "-am", "legacy")
    _git(env.other, "push", "-q", "origin", "HEAD:master")
    sync = env.make_sync()
    res = sync.submit(operations.make_add("emag", "x1"))
    assert res.state == "invalid"
    assert "legacy" in res.detail
    assert res.pending_count == 1


# --- serialization ---------------------------------------------------------


def test_concurrent_submits_are_serialized(env: Env):
    sync = env.make_sync()
    results: list[Any] = []

    def go(query: str) -> None:
        results.append(sync.submit(operations.make_add("emag", query)))

    threads = [threading.Thread(target=go, args=(q,)) for q in ("t one", "t two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.state for r in results) == ["pushed", "pushed"]
    queries = {w["query"] for w in env.origin_doc()["watches"]}
    assert {"t one", "t two"} <= queries
    subjects = env.origin_subjects()
    assert sum(s.startswith("chore(watchlist): add") for s in subjects) == 2
    merges = _git(env.origin, "rev-list", "--merges", "--count", "master")
    assert merges == "0"  # linear history
    assert sync.status().pending_count == 0


def test_submit_fails_fast_when_another_process_holds_the_clone(env: Env):
    holder_code = (
        "import sys\nfrom pathlib import Path\n"
        "from bf_price_monitor.dashboard.locking import CloneLock\n"
        "with CloneLock(Path(sys.argv[1])):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    sync = env.make_sync()
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(sync.lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(locking.CloneBusy):
            sync.submit(operations.make_add("emag", "blocked"))
        assert sync.status().pending_count == 0  # nothing journaled
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=10)


def test_journal_lives_inside_dot_git_so_the_work_tree_stays_clean(env: Env):
    env.take_origin_offline()
    sync = env.make_sync()
    sync.submit(operations.make_add("emag", "kept one"))
    assert isinstance(sync.journal, journal.Journal)
    assert (env.clone / ".git" / "bf-dashboard" / "pending.jsonl").is_file()
    assert _git(env.clone, "status", "--porcelain") == ""
