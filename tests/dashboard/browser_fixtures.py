"""Shared fixtures for the browser tests (imported by name, not a conftest,
so the root tests/conftest.py stays the only one): a live dashboard server on a scratch
origin, and one Chromium page. Only requested by tests marked ``browser``."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, sync_playwright
from scratch_repo import TOKEN, Scratch, build_scratch

from bf_price_monitor.dashboard import server


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
