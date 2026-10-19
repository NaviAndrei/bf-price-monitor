"""Slice C: the static page is served locally and offers only supported fields."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from scratch_repo import Scratch, build_scratch

from bf_price_monitor.dashboard import operations, server

TOKEN = "unit-test-token"
PORT = 8765
STATIC = Path(server.__file__).parent / "static"
FILES = ("index.html", "app.js", "app.css")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    scratch: Scratch = build_scratch(tmp_path)
    app = server.create_app(scratch.cfg, token=TOKEN, port=PORT)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:
        yield c


def _text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_index_and_assets_are_served_with_the_strict_policy(client: TestClient):
    for path, kind in (
        ("/", "text/html"),
        ("/static/app.js", "javascript"),
        ("/static/app.css", "text/css"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert kind in response.headers["content-type"]
        assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "Watchlist" in client.get("/").text


def test_no_external_assets_inline_script_or_inline_style():
    for name in FILES:
        text = _text(name)
        assert not re.search(r"https?://|//cdn|@import|url\(", text), name
    html = _text("index.html")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\sstyle=|\son\w+=", html)
    assert "innerHTML" not in _text("app.js")
    assert "eval(" not in _text("app.js")


def test_forms_offer_only_fields_with_an_operation_behind_them():
    html = _text("index.html")
    names = set(re.findall(r'<(?:input|select)[^>]*\bname="(\w+)"', html))
    assert names == {"site", "query", "target_price", "min_drop_percent"}
    js = _text("app.js")
    editable = re.search(r"const EDITABLE = \[(.*?)\]", js, re.S)
    assert editable is not None
    fields = set(re.findall(r'"(\w+)"', editable.group(1)))
    assert fields == set(operations.EDITABLE_FIELDS)


def test_dead_dials_are_read_only_text_never_inputs():
    html, js = _text("index.html"), _text("app.js")
    for dial in (
        "cadence_minutes",
        "track_all_time_low",
        "drop_rule",
        "drop_threshold",
    ):
        assert f'name="{dial}"' not in html
        assert dial in js  # listed under "not used yet", read-only
    unused = re.search(r"const UNUSED = \[(.*?)\]", js, re.S)
    assert unused is not None and "cadence_minutes" in unused.group(1)


def test_page_sends_the_token_only_on_writes_and_keeps_it_out_of_the_url():
    js = _text("app.js")
    assert 'init.method !== "GET"' in js
    assert "history.replaceState" in js
    assert "localStorage" not in js and "sessionStorage" not in js
    assert "document.cookie" not in js


def test_page_never_overwrites_after_a_409():
    js = _text("app.js")
    assert "status === 409" in js
    assert "Reload" in js


def test_static_mount_cannot_escape_its_directory(client: TestClient):
    for path in (
        "/static/../server.py",
        "/static/%2e%2e/server.py",
        "/static/..%5cserver.py",
    ):
        assert client.get(path).status_code in (400, 404)


def test_page_does_not_expose_the_write_token(client: TestClient):
    for path in ("/", "/static/app.js", "/static/app.css"):
        assert TOKEN not in client.get(path).text
