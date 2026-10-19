"""Slice C: launcher behaviour and the clone-state rule (D3), on scratch clones."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from scratch_repo import Scratch, build_scratch, git

from bf_price_monitor.dashboard import server


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    return build_scratch(tmp_path)


def _env(s: Scratch, **extra: str) -> dict[str, str]:
    return {
        "BF_DASHBOARD_CLONE": str(s.clone),
        "BF_DASHBOARD_GIT_EMAIL": "dash@example.com",
        **extra,
    }


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        server.uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw})
    )
    return calls


def test_main_binds_loopback_only_and_prints_the_token_to_stdout_only(
    scratch: Scratch,
    uvicorn_calls: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.DEBUG)
    assert server.main(["--port", "9123"], _env(scratch)) == 0
    out, err = capsys.readouterr()
    match = re.search(r"http://127\.0\.0\.1:9123/#token=([\w-]{32,})", out)
    assert match, out
    token = match.group(1)
    assert token not in err
    assert token not in caplog.text
    (call,) = uvicorn_calls
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 9123
    assert call["access_log"] is False
    assert call["proxy_headers"] is False


def test_main_uses_a_new_token_each_launch(
    scratch: Scratch,
    uvicorn_calls: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
):
    server.main([], _env(scratch))
    first = capsys.readouterr().out
    server.main([], _env(scratch))
    second = capsys.readouterr().out
    assert first != second


@pytest.mark.parametrize("port", ["80", "70000", "abc"])
def test_main_rejects_bad_ports(port: str, scratch: Scratch):
    with pytest.raises(SystemExit) as exc:
        server.main(["--port", port], _env(scratch))
    assert exc.value.code == 2


def test_main_refuses_without_a_clone_setting(
    capsys: pytest.CaptureFixture[str], uvicorn_calls: list[dict[str, Any]]
):
    assert server.main([], {}) == 2
    assert "BF_DASHBOARD_CLONE" in capsys.readouterr().err
    assert uvicorn_calls == []


def test_main_ignores_environment_outside_the_dashboard_prefix(
    scratch: Scratch,
    uvicorn_calls: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    env = _env(scratch, TELEGRAM_BOT_TOKEN="secret-should-not-be-read")
    seen: dict[str, str] = {}
    real = server.load_config

    def spy(mapping: Any, runner: Any) -> Any:
        seen.update(mapping)
        return real(mapping, runner)

    monkeypatch.setattr(server, "load_config", spy)
    assert server.main([], env) == 0
    assert "TELEGRAM_BOT_TOKEN" not in seen
    assert "secret-should-not-be-read" not in capsys.readouterr().out


# --- D3: clone state ---------------------------------------------------------


def test_fresh_clone_on_master_is_ready(scratch: Scratch):
    server.check_clone_ready(scratch.cfg)


def test_detached_clone_is_ready(scratch: Scratch):
    git(scratch.clone, "switch", "-q", "--detach", "HEAD")
    server.check_clone_ready(scratch.cfg)


def test_dirty_clone_is_refused(scratch: Scratch):
    path = scratch.clone / "data" / "watchlist.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(server.CloneNotReady, match="uncommitted"):
        server.check_clone_ready(scratch.cfg)


def test_clone_on_another_branch_is_refused(scratch: Scratch):
    git(scratch.clone, "update-ref", "refs/heads/other", "HEAD")
    git(scratch.clone, "symbolic-ref", "HEAD", "refs/heads/other")
    with pytest.raises(server.CloneNotReady, match="not master"):
        server.check_clone_ready(scratch.cfg)


def test_missing_clone_directory_is_refused(tmp_path: Path, scratch: Scratch):
    cfg = scratch.cfg.__class__(tmp_path / "nope", "a", "a@example.com")
    with pytest.raises(server.CloneNotReady):
        server.check_clone_ready(cfg)


def test_main_refuses_a_dirty_clone(
    scratch: Scratch,
    uvicorn_calls: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
):
    path = scratch.clone / "data" / "watchlist.json"
    path.write_text("[]", encoding="utf-8")
    assert server.main([], _env(scratch)) == 2
    assert uvicorn_calls == []
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "git restore --staged --worktree" in err  # tells you how to recover
