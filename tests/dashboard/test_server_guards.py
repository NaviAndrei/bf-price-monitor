"""Slice C, S11: the local-server guards. Written before server.py."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bf_price_monitor.dashboard import server
from bf_price_monitor.dashboard.config import DashboardConfig

TOKEN = "unit-test-token"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    cfg = DashboardConfig(tmp_path / "clone", "Dash", "dash@example.com")
    application = server.create_app(cfg, token=TOKEN, port=PORT)

    @application.get("/probe")
    def probe_get() -> dict[str, bool]:
        return {"ok": True}

    for method in WRITE_METHODS:
        application.add_api_route(
            "/probe", lambda: {"ok": True}, methods=[method], name=f"probe_{method}"
        )
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=BASE) as c:
        yield c


def _write(client: TestClient, method: str, **kw: Any) -> Any:
    headers = {server.TOKEN_HEADER: TOKEN, "content-type": "application/json"}
    headers.update(kw.pop("headers", {}))
    return client.request(method, "/probe", headers=headers, content=b"{}", **kw)


# --- Host header -----------------------------------------------------------


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}"])
def test_allowed_hosts_pass(client: TestClient, host: str):
    assert client.get("/probe", headers={"host": host}).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        f"evil.example:{PORT}",
        f"127.0.0.1.evil.example:{PORT}",
        f"localhost.evil.example:{PORT}",
        "127.0.0.1:9999",  # wrong port: TrustedHostMiddleware alone lets this by
        "localhost:80",
        "127.0.0.1",  # no port at all
        f"[::1]:{PORT}",
        f"0.0.0.0:{PORT}",
    ],
)
def test_foreign_or_wrong_port_host_is_rejected(client: TestClient, host: str):
    response = client.get("/probe", headers={"host": host})
    assert response.status_code == 400
    assert host not in response.text  # the offending host is never echoed


def test_rejected_host_runs_before_the_token_check(client: TestClient):
    response = _write(client, "POST", headers={"host": "evil.example"})
    assert response.status_code == 400


# --- token -----------------------------------------------------------------


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_write_without_token_is_rejected(client: TestClient, method: str):
    response = client.request(
        method, "/probe", headers={"content-type": "application/json"}, content=b"{}"
    )
    assert response.status_code == 403


@pytest.mark.parametrize("method", WRITE_METHODS)
@pytest.mark.parametrize("token", ["", "wrong", TOKEN + "x", TOKEN[:-1], TOKEN.upper()])
def test_write_with_wrong_token_is_rejected(
    client: TestClient, method: str, token: str
):
    response = _write(client, method, headers={server.TOKEN_HEADER: token})
    assert response.status_code == 403


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_write_with_the_right_token_passes(client: TestClient, method: str):
    assert _write(client, method).status_code == 200


def test_reads_do_not_need_the_token(client: TestClient):
    assert client.get("/probe").status_code == 200


def test_token_is_compared_in_constant_time():
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "compare_digest" in source


# --- content type ----------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "text/plain; charset=utf-8",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "application/jsonx",
        "application/vnd.api+json",
        "",
    ],
)
def test_non_json_content_type_is_rejected(client: TestClient, content_type: str):
    response = _write(client, "POST", headers={"content-type": content_type})
    assert response.status_code == 415


def test_missing_content_type_is_rejected(client: TestClient):
    response = client.post(
        "/probe", headers={server.TOKEN_HEADER: TOKEN}, content=b"{}"
    )
    # httpx adds no content-type for raw bytes
    assert response.status_code == 415


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", "APPLICATION/JSON"],
)
def test_json_content_types_pass(client: TestClient, content_type: str):
    response = _write(client, "POST", headers={"content-type": content_type})
    assert response.status_code == 200


def test_token_is_checked_before_content_type(client: TestClient):
    response = client.post(
        "/probe", headers={"content-type": "text/plain"}, content=b"x"
    )
    assert response.status_code == 403


# --- CORS and hardening ----------------------------------------------------


def _cors_headers(response: Any) -> list[str]:
    return [h for h in response.headers if h.lower().startswith("access-control-")]


REQUESTS: list[Callable[[TestClient], Any]] = [
    lambda c: c.get("/probe"),
    lambda c: c.get("/probe", headers={"origin": "https://evil.example"}),
    lambda c: c.get("/probe", headers={"host": "evil.example"}),
    lambda c: c.get("/nope"),
    lambda c: c.post("/probe", content=b"{}"),
    lambda c: _write(c, "POST", headers={"origin": "https://evil.example"}),
    lambda c: c.options(
        "/probe",
        headers={
            "origin": "https://evil.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    ),
]


@pytest.mark.parametrize("make_request", REQUESTS)
def test_no_response_carries_cors_headers(
    client: TestClient, make_request: Callable[[TestClient], Any]
):
    assert _cors_headers(make_request(client)) == []


def test_preflight_is_not_answered_with_permission(client: TestClient):
    response = client.options(
        "/probe",
        headers={
            "origin": "https://evil.example",
            "access-control-request-method": "POST",
        },
    )
    assert response.status_code >= 400


def test_responses_carry_hardening_headers(client: TestClient):
    headers = client.get("/probe").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    csp = headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp


def test_interactive_docs_are_not_served(client: TestClient):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- S9: nothing sensitive is echoed or logged -----------------------------


def test_token_and_headers_are_never_echoed_or_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
):
    canary = "canary-header-value"
    caplog.set_level(logging.DEBUG)
    responses = [
        client.get("/probe", headers={"x-canary": canary}),
        client.get("/probe", headers={"host": "evil.example", "x-canary": canary}),
        client.post(
            "/probe",
            headers={server.TOKEN_HEADER: "wrong-token", "x-canary": canary},
            content=b"{}",
        ),
        _write(client, "POST", headers={"x-canary": canary}),
        _write(client, "POST", headers={"content-type": "text/plain"}),
    ]
    for response in responses:
        text = response.text + str(dict(response.headers))
        assert canary not in text
        assert TOKEN not in text
        assert "wrong-token" not in text
    assert TOKEN not in caplog.text
    assert canary not in caplog.text


def test_new_token_is_random_and_long():
    tokens = {server.new_token() for _ in range(5)}
    assert len(tokens) == 5
    assert all(len(t) >= 32 for t in tokens)


def test_server_does_not_import_the_pipeline_or_read_secret_env():
    import ast

    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not {m for m in imported if m.split(".")[-1] in {"analyze", "notify"}}
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "os.getenv" not in source
    assert source.count("os.environ") <= 1  # only main()'s default mapping
