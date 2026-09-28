"""Always-on entrypoint for Docker mode (T-38, #38).

    python scripts/run_loop.py run          # loop forever (container default)
    python scripts/run_loop.py once         # one scrape -> analyze -> notify cycle
    python scripts/run_loop.py selfcheck    # offline: writable data/, SQLite, Chromium
    python scripts/run_loop.py healthcheck  # Docker HEALTHCHECK: loop heartbeat is fresh

Each stage is the same script monitor.yml runs, as its own child process,
so Docker and GitHub Actions share one pipeline. A failed stage ends that
cycle early (like a failed workflow step). SIGTERM/SIGINT stop scheduling
new work, forward SIGTERM to the running stage, and exit 0 once it ends.

Environment:
    MONITOR_INTERVAL_MINUTES       default 120 (the monitor.yml cron cadence)
    MONITOR_STAGES                 default "scrape,analyze,notify"
    MONITOR_STAGE_TIMEOUT_MINUTES  default 45; a stage running longer is killed
    MONITOR_HEARTBEAT_FILE         default /tmp/bf-monitor.heartbeat
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR.parent))

STAGE_SCRIPTS = {
    "scrape": SCRIPTS_DIR / "scrape.py",
    "analyze": SCRIPTS_DIR / "analyze.py",
    "notify": SCRIPTS_DIR / "notify.py",
}
DATA_DIR = Path("data")
DEFAULT_HEARTBEAT = Path("/tmp/bf-monitor.heartbeat")
HEARTBEAT_EVERY_SECONDS = 30.0
# Healthy while the loop itself is alive: the heartbeat is refreshed every
# HEARTBEAT_EVERY_SECONDS even while a long scrape runs.
HEARTBEAT_MAX_AGE_SECONDS = 300.0
TERMINATE_GRACE_SECONDS = 30.0


def _log(message: str) -> None:
    print(f"[run_loop] {message}", flush=True)


class Settings:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.interval_seconds = 60.0 * float(env.get("MONITOR_INTERVAL_MINUTES") or 120)
        self.stage_timeout_seconds = 60.0 * float(
            env.get("MONITOR_STAGE_TIMEOUT_MINUTES") or 45
        )
        raw = env.get("MONITOR_STAGES") or "scrape,analyze,notify"
        self.stages = [s.strip() for s in raw.split(",") if s.strip()]
        unknown = [
            s for s in self.stages if s not in STAGE_SCRIPTS and s != "selfcheck"
        ]
        if unknown or not self.stages:
            raise SystemExit(f"[run_loop] invalid MONITOR_STAGES: {raw!r}")
        self.heartbeat = Path(env.get("MONITOR_HEARTBEAT_FILE") or DEFAULT_HEARTBEAT)
        self.telegram_configured = bool(
            (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
            and (env.get("TELEGRAM_CHAT_ID") or "").strip()
        )


def touch_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.time():.0f}\n", encoding="utf-8")


def heartbeat_is_fresh(path: Path, *, now: float | None = None) -> bool:
    try:
        stamp = float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return ((time.time() if now is None else now) - stamp) <= HEARTBEAT_MAX_AGE_SECONDS


class Runner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop = threading.Event()
        self._child: subprocess.Popen[bytes] | None = None

    def request_stop(self, signum: int, _frame: object = None) -> None:
        if not self.stop.is_set():
            _log(f"signal {signum} received; finishing current stage then stopping")
        self.stop.set()
        child = self._child
        if child is not None and child.poll() is None:
            child.terminate()

    def run_stage(self, stage: str) -> bool:
        if stage == "selfcheck":
            return selfcheck() == 0
        if stage == "notify" and not self.settings.telegram_configured:
            _log("notify skipped: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
            return True
        started = time.monotonic()
        self._child = subprocess.Popen([sys.executable, str(STAGE_SCRIPTS[stage])])
        try:
            while True:
                try:
                    code = self._child.wait(timeout=HEARTBEAT_EVERY_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    touch_heartbeat(self.settings.heartbeat)
                    if time.monotonic() - started > self.settings.stage_timeout_seconds:
                        _log(f"{stage} exceeded its timeout; terminating")
                        self._child.terminate()
                        try:
                            code = self._child.wait(timeout=TERMINATE_GRACE_SECONDS)
                        except subprocess.TimeoutExpired:
                            self._child.kill()
                            code = self._child.wait()
                        break
        finally:
            self._child = None
        _log(f"{stage} exited with code {code}")
        return code == 0

    def run_cycle(self) -> bool:
        for stage in self.settings.stages:
            if self.stop.is_set():
                return False
            if not self.run_stage(stage):
                _log(f"cycle ended early: {stage} failed")
                return False
        return True

    def loop(self) -> int:
        _log(
            f"started: stages={','.join(self.settings.stages)} "
            f"interval={self.settings.interval_seconds / 60:.0f}min"
        )
        while not self.stop.is_set():
            touch_heartbeat(self.settings.heartbeat)
            self.run_cycle()
            deadline = time.monotonic() + self.settings.interval_seconds
            while not self.stop.is_set():
                touch_heartbeat(self.settings.heartbeat)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.stop.wait(min(HEARTBEAT_EVERY_SECONDS, remaining))
        _log("stopped")
        return 0


def selfcheck() -> int:
    """No-network readiness check: data/ is writable, the SQLite store opens
    and migrates, the bundled watchlist validates, and headless Chromium
    launches and renders a page."""
    from bf_price_monitor.config.validator import validate_watchlist
    from bf_price_monitor.storage import sqlite as sqlite_storage

    probe = DATA_DIR / ".selfcheck"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    conn = sqlite_storage.init_db(DATA_DIR / "price_history.db")
    try:
        version = sqlite_storage._user_version(conn)
    finally:
        conn.close()
    validate_watchlist(DATA_DIR / "watchlist.json")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content("<p id='probe'>bf-price-monitor</p>")
            text = page.locator("#probe").inner_text()
        finally:
            browser.close()
    if text != "bf-price-monitor":
        _log("selfcheck failed: Chromium did not render the probe page")
        return 1
    _log(
        f"selfcheck ok: data writable, sqlite schema v{version}, watchlist valid, chromium ok"
    )
    return 0


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "run"
    settings = Settings(os.environ if env is None else env)
    if command == "healthcheck":
        return 0 if heartbeat_is_fresh(settings.heartbeat) else 1
    if command == "selfcheck":
        return selfcheck()
    runner = Runner(settings)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    if command == "once":
        return 0 if runner.run_cycle() else 1
    if command == "run":
        return runner.loop()
    _log(f"unknown command {command!r}; use run, once, selfcheck or healthcheck")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
