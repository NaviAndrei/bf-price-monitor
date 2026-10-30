"""Tests for .claude/hooks/verify_completion.py (Stop hook, two modes)."""

import importlib.util
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "hooks"
    / "verify_completion.py"
)
_spec = importlib.util.spec_from_file_location("verify_completion", HOOK_PATH)
vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vc)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV},
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


class Runner:
    def __init__(self, code=0, tail="boom"):
        self.code, self.tail, self.calls = code, tail, 0

    def __call__(self, cwd):
        self.calls += 1
        return self.code, self.tail


def test_green_run_is_cached_so_an_unchanged_tree_is_not_retested(repo):
    runner = Runner(0)
    assert vc.check_tests(str(repo), runner) == (0, "")
    assert vc.check_tests(str(repo), runner) == (0, "")
    assert runner.calls == 1


def test_a_changed_tree_runs_again(repo):
    runner = Runner(0)
    vc.check_tests(str(repo), runner)
    (repo / "scripts" / "a.py").write_text("x = 2\n")
    vc.check_tests(str(repo), runner)
    assert runner.calls == 2


def test_a_new_untracked_python_file_changes_the_key(repo):
    before = vc.tree_key(str(repo))
    (repo / "scripts" / "b.py").write_text("y = 1\n")
    assert vc.tree_key(str(repo)) != before


def test_red_run_exits_2_with_the_tail(repo):
    code, message = vc.check_tests(str(repo), Runner(1, "FAILED test_x"))
    assert code == 2
    assert "pytest exited 1" in message and "FAILED test_x" in message


def test_second_failure_on_the_same_tree_does_not_wake_again(repo):
    runner = Runner(1)
    assert vc.check_tests(str(repo), runner)[0] == 2
    code, message = vc.check_tests(str(repo), runner)
    assert code == 0 and "not waking" in message
    assert runner.calls == 2


def test_a_held_lock_skips_the_run(repo):
    state = repo / vc.STATE_DIR
    state.mkdir(parents=True)
    (state / vc.LOCK_NAME).write_text("")
    runner = Runner(0)
    code, message = vc.check_tests(str(repo), runner)
    assert code == 0 and "in progress" in message
    assert runner.calls == 0


def test_a_stale_lock_is_taken_over(repo):
    state = repo / vc.STATE_DIR
    state.mkdir(parents=True)
    lock = state / vc.LOCK_NAME
    lock.write_text("")
    old = time.time() - vc.LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))
    runner = Runner(0)
    assert vc.check_tests(str(repo), runner) == (0, "")
    assert runner.calls == 1
    assert not lock.exists()


def test_unknown_result_does_not_block_or_cache(repo):
    runner = Runner(None, "pytest exceeded 600s; result unknown")
    code, message = vc.check_tests(str(repo), runner)
    assert code == 0 and "not blocking" in message
    assert vc.read_cache(repo / vc.STATE_DIR) == {}


def test_old_commit_and_clean_tree_runs_nothing(repo):
    _git(
        repo, "commit", "-q", "--allow-empty", "--date=2020-01-01T00:00:00", "-m", "old"
    )
    env = {**os.environ, **GIT_ENV, "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
    subprocess.run(
        ["git", "commit", "-q", "--amend", "--allow-empty", "--no-edit"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    runner = Runner(0)
    assert vc.check_tests(str(repo), runner) == (0, "")
    assert runner.calls == 0


def test_not_a_git_repo_is_ignored():
    # tmp_path lives under this repo (pytest basetemp is .pytest_tmp), so git
    # would find the repo's history there; use the system temp folder instead.
    with tempfile.TemporaryDirectory() as outside:
        assert vc.check_tests(outside, Runner(0)) == (0, "")
        assert vc.check_progress(outside) == []


def test_progress_mode_flags_code_commit_without_the_handoff_log(repo):
    (repo / "scripts" / "a.py").write_text("x = 3\n")
    _git(repo, "commit", "-q", "-am", "code change")
    problems = vc.check_progress(str(repo))
    assert len(problems) == 1 and "docs/progress.md" in problems[0]
    (repo / "docs").mkdir()
    (repo / "docs" / "progress.md").write_text("log\n")
    (repo / "scripts" / "a.py").write_text("x = 4\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "code and log")
    assert vc.check_progress(str(repo)) == []
