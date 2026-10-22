"""Browser smoke test: the dashboard page run in real Chromium.

The other dashboard tests check the page's source text and call the API
directly. This one loads the page, follows the token link, toggles a watch and
checks the commit that reaches the scratch origin, so it catches a broken
script that the source checks cannot see.

Opt-in: excluded from the default run (see ``addopts`` in pyproject.toml).
Run it with ``.venv/Scripts/python.exe -m pytest -m browser``. It needs the
Chromium build that ``playwright install chromium`` installs.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, sync_playwright
from scratch_repo import Scratch, build_scratch

from bf_price_monitor.dashboard import server

pytestmark = pytest.mark.browser

TOKEN = "browser-smoke-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def running_dashboard(tmp_path: Path) -> Iterator[tuple[Scratch, str]]:
    scratch = build_scratch(tmp_path)
    port = _free_port()
    app = server.create_app(scratch.cfg, token=TOKEN, port=port)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if uvicorn_server.started:
            break
        thread.join(0.05)
    assert uvicorn_server.started, "dashboard server did not start"
    try:
        yield scratch, f"http://127.0.0.1:{port}"
    finally:
        uvicorn_server.should_exit = True
        thread.join(5)


@pytest.fixture
def page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            yield browser.new_page()
        finally:
            browser.close()


def test_page_loads_from_the_token_link_and_toggles_a_watch(
    running_dashboard: tuple[Scratch, str], page: Page
):
    scratch, base = running_dashboard
    page.goto(f"{base}/#token={TOKEN}")

    rows = page.locator("#watch-rows tr")
    rows.first.wait_for()
    assert rows.count() == 2
    # The token leaves the address bar once the page has read it.
    assert "token=" not in page.url

    rows.first.get_by_role("button", name="On").click()
    # The save is a push to the scratch origin; wait for that commit to land.
    for _ in range(100):
        subjects = scratch.origin_subjects()
        if subjects[0].startswith("chore(watchlist): disable"):
            break
        page.wait_for_timeout(100)
    assert subjects[0].startswith("chore(watchlist): disable"), subjects
    doc = scratch.origin_doc()
    assert sum(1 for watch in doc["watches"] if watch["enabled"]) == 1
