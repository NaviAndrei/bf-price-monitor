"""T-38 (#38): Docker-mode loop entrypoint. Stages are replaced with tiny
stand-in scripts in tmp_path, so nothing scrapes, sends or touches data/."""

from __future__ import annotations

import signal
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import run_loop

REPO = Path(__file__).resolve().parent.parent


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


@pytest.fixture
def stages(tmp_path, monkeypatch):
    log = tmp_path / "ran.txt"
    scripts = {
        name: _script(
            tmp_path,
            name,
            f"""
            from pathlib import Path
            with Path({str(log)!r}).open("a") as f:
                f.write("{name}\\n")
            """,
        )
        for name in ("scrape", "analyze", "notify")
    }
    monkeypatch.setattr(run_loop, "STAGE_SCRIPTS", scripts)
    return scripts, log


def _settings(tmp_path, **env):
    base = {
        "MONITOR_HEARTBEAT_FILE": str(tmp_path / "hb"),
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "1",
    }
    return run_loop.Settings({**base, **env})


def _ran(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


def test_cycle_runs_stages_in_pipeline_order(stages, tmp_path):
    _, log = stages
    assert run_loop.Runner(_settings(tmp_path)).run_cycle() is True
    assert _ran(log) == ["scrape", "analyze", "notify"]


def test_failed_stage_ends_the_cycle_early(stages, tmp_path):
    scripts, log = stages
    scripts["scrape"].write_text("raise SystemExit(3)\n", encoding="utf-8")
    assert run_loop.Runner(_settings(tmp_path)).run_cycle() is False
    assert _ran(log) == []


def test_notify_is_skipped_without_telegram_credentials(stages, tmp_path, capsys):
    _, log = stages
    settings = run_loop.Settings({"MONITOR_HEARTBEAT_FILE": str(tmp_path / "hb")})
    assert run_loop.Runner(settings).run_cycle() is True
    assert _ran(log) == ["scrape", "analyze"]
    assert "notify skipped" in capsys.readouterr().out


def test_stage_exceeding_timeout_is_terminated(stages, tmp_path, monkeypatch):
    scripts, _ = stages
    scripts["scrape"].write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(run_loop, "HEARTBEAT_EVERY_SECONDS", 0.2)
    settings = _settings(tmp_path, MONITOR_STAGES="scrape")
    settings.stage_timeout_seconds = 0.5
    started = time.monotonic()
    assert run_loop.Runner(settings).run_cycle() is False
    assert time.monotonic() - started < 20
    assert run_loop.heartbeat_is_fresh(settings.heartbeat)


def test_stop_request_terminates_running_stage_and_loop_exits_zero(
    stages, tmp_path, monkeypatch
):
    scripts, _ = stages
    scripts["scrape"].write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(run_loop, "HEARTBEAT_EVERY_SECONDS", 0.2)
    runner = run_loop.Runner(_settings(tmp_path, MONITOR_STAGES="scrape"))
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", runner.loop()))
    thread.start()
    deadline = time.monotonic() + 10
    while runner._child is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runner._child is not None
    runner.request_stop(signal.SIGTERM)
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert result["code"] == 0


def test_loop_waits_between_cycles_and_refreshes_heartbeat(
    stages, tmp_path, monkeypatch
):
    _, log = stages
    monkeypatch.setattr(run_loop, "HEARTBEAT_EVERY_SECONDS", 0.05)
    settings = _settings(tmp_path, MONITOR_STAGES="scrape")
    settings.interval_seconds = 0.2
    runner = run_loop.Runner(settings)
    thread = threading.Thread(target=runner.loop)
    thread.start()
    deadline = time.monotonic() + 15
    while len(_ran(log)) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    runner.request_stop(signal.SIGTERM)
    thread.join(timeout=15)
    assert len(_ran(log)) >= 2
    assert run_loop.heartbeat_is_fresh(settings.heartbeat)


def test_heartbeat_freshness(tmp_path):
    hb = tmp_path / "hb"
    assert not run_loop.heartbeat_is_fresh(hb)
    run_loop.touch_heartbeat(hb)
    assert run_loop.heartbeat_is_fresh(hb)
    stale = time.time() + run_loop.HEARTBEAT_MAX_AGE_SECONDS + 1
    assert not run_loop.heartbeat_is_fresh(hb, now=stale)
    hb.write_text("garbage")
    assert not run_loop.heartbeat_is_fresh(hb)


def test_healthcheck_command_exit_codes(tmp_path):
    env = {"MONITOR_HEARTBEAT_FILE": str(tmp_path / "hb")}
    assert run_loop.main(["healthcheck"], env=env) == 1
    run_loop.touch_heartbeat(tmp_path / "hb")
    assert run_loop.main(["healthcheck"], env=env) == 0


@pytest.mark.parametrize("stages_value", ["scrape,deploy", ","])
def test_invalid_stage_list_is_rejected(tmp_path, stages_value):
    with pytest.raises(SystemExit):
        run_loop.Settings({"MONITOR_STAGES": stages_value})


def test_defaults_match_monitor_yml_cadence():
    settings = run_loop.Settings({})
    assert settings.interval_seconds == 120 * 60
    assert settings.stages == ["scrape", "analyze", "notify"]
    assert settings.heartbeat == run_loop.DEFAULT_HEARTBEAT


def test_unknown_command_returns_usage_error(tmp_path, monkeypatch):
    monkeypatch.setattr(run_loop.signal, "signal", lambda *a: None)
    assert run_loop.main(["deploy"], env={}) == 2


def test_selfcheck_stage_passes_on_a_writable_data_dir(tmp_path, monkeypatch):
    # Real Chromium launch: requires `playwright install chromium`, which the
    # dev setup and CI both do; skip rather than fail where it's absent.
    pytest.importorskip("playwright.sync_api")
    data = tmp_path / "data"
    data.mkdir()
    (data / "watchlist.json").write_text(
        (REPO / "data" / "watchlist.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(run_loop, "DATA_DIR", data)
    try:
        code = run_loop.selfcheck()
    except Exception as exc:  # noqa: BLE001
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Chromium not installed for Playwright")
        raise
    assert code == 0
    assert (data / "price_history.db").is_file()
    assert not (data / ".selfcheck").exists()


def test_dockerignore_is_an_allowlist_that_excludes_state_and_secrets():
    lines = [
        line.strip()
        for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    allowed = {line[1:] for line in lines if line.startswith("!")}
    assert allowed == {
        "pyproject.toml",
        "uv.lock",
        "bf_price_monitor/",
        "scripts/*.py",
        "data/watchlist.json",
    }


def test_dockerfile_hardening_invariants():
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "uv sync --frozen" in text
    assert "--only-shell chromium" in text
    assert "USER 10001:10001" in text
    assert '"tini", "--"' in text
    assert "HEALTHCHECK" in text
    for line in text.splitlines():
        if line.startswith(("FROM ", "ARG PYTHON_IMAGE")) and "${" not in line:
            assert "@sha256:" in line, line
    assert "--extra" not in text  # no dev/anomaly extras in the image


def test_compose_hardening_invariants():
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    for needle in (
        "read_only: true",
        "no-new-privileges:true",
        "- ALL",
        "/tmp:size=",
        "monitor-data:/app/data",
        "restart: unless-stopped",
        "stop_grace_period",
        "required: false",
    ):
        assert needle in text, needle


def test_github_actions_monitor_does_not_depend_on_docker():
    monitor = (REPO / ".github/workflows/monitor.yml").read_text(encoding="utf-8")
    assert "docker" not in monitor.lower()
    assert "run_loop.py" not in monitor


def test_env_example_has_no_values_for_secrets():
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            if key != "MONITOR_INTERVAL_MINUTES":
                assert value == "", key


def test_run_loop_is_importable_without_side_effects():
    assert sys.modules["run_loop"].main is run_loop.main
