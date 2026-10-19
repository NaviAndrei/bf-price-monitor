"""Slice C: read routes (watchlist, pending, conflicts, health)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from scratch_repo import WATCHLIST, Scratch, build_scratch, git, seed_doc

from bf_price_monitor.dashboard import operations, server
from bf_price_monitor.dashboard.sites import CHANNELS, KNOWN_SITES

TOKEN = "unit-test-token"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    return build_scratch(tmp_path)


@pytest.fixture
def client(scratch: Scratch) -> Iterator[TestClient]:
    app = server.create_app(scratch.cfg, token=TOKEN, port=PORT)
    with TestClient(app, base_url=BASE) as c:
        yield c


def _sync(client: TestClient) -> Any:
    return client.app.state.sync  # type: ignore[attr-defined]


def test_watchlist_returns_the_watches_etag_and_menus(
    client: TestClient, scratch: Scratch
):
    body = client.get("/api/watchlist").json()
    assert body["watches"] == seed_doc()["watches"]
    raw = (scratch.clone / WATCHLIST).read_bytes()
    assert body["etag"] == hashlib.sha256(raw).hexdigest()
    assert body["sites"] == list(KNOWN_SITES)
    assert body["channels"] == list(CHANNELS)
    assert body["pending_count"] == 0
    assert body["pending"] == []
    assert body["conflicts"] == []
    assert body["clone"]["ok"] is True


def test_etag_is_stable_and_follows_the_file_bytes(
    client: TestClient, scratch: Scratch
):
    first = client.get("/api/watchlist").json()["etag"]
    assert client.get("/api/watchlist").json()["etag"] == first
    git(scratch.clone, "switch", "-q", "--detach", "HEAD")
    path = scratch.clone / WATCHLIST
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert client.get("/api/watchlist").json()["etag"] != first


def test_pending_ops_show_up_in_watchlist_and_pending(client: TestClient):
    op = operations.make_add("flanco", "monitor lg")
    _sync(client).journal.append(op)
    pending = client.get("/api/pending").json()
    assert pending["pending_count"] == 1
    assert pending["pending"][0]["op"] == op
    assert "op_id" in pending["pending"][0]
    assert client.get("/api/watchlist").json()["pending_count"] == 1


def test_conflicts_show_up_with_their_reason(client: TestClient):
    entry = _sync(client).journal.append(operations.make_add("flanco", "monitor lg"))
    _sync(client).journal.move_to_conflicts(entry, reason="boom", kind="conflict")
    conflicts = client.get("/api/conflicts").json()["conflicts"]
    assert [c["reason"] for c in conflicts] == ["boom"]
    assert client.get("/api/watchlist").json()["conflicts"] == conflicts


def test_reads_need_no_token_but_do_need_a_valid_host(client: TestClient):
    for path in ("/api/watchlist", "/api/pending", "/api/conflicts", "/api/health"):
        assert client.get(path).status_code == 200
        assert client.get(path, headers={"host": "evil.example"}).status_code == 400


def test_corrupt_journal_is_reported_not_swallowed(
    client: TestClient, scratch: Scratch
):
    pending = scratch.clone / ".git" / "bf-dashboard" / "pending.jsonl"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("{not json\n", encoding="utf-8")
    response = client.get("/api/pending")
    assert response.status_code == 500
    assert "corrupt" in response.json()["error"]


def test_unreadable_watchlist_is_a_503_with_the_reason(
    client: TestClient, scratch: Scratch
):
    git(scratch.clone, "switch", "-q", "--detach", "HEAD")
    (scratch.clone / WATCHLIST).write_text("[]", encoding="utf-8")
    response = client.get("/api/watchlist")
    assert response.status_code == 503
    assert "legacy" in response.json()["error"]


def test_health_of_a_fresh_clone(client: TestClient):
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert health["branch"] == "master"
    assert health["tree_clean"] is True
    assert len(health["head"]) >= 7


def test_health_of_a_detached_clone_is_still_ok(client: TestClient, scratch: Scratch):
    git(scratch.clone, "switch", "-q", "--detach", "HEAD")
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert health["branch"] == "detached"


def test_health_reports_a_dirty_clone(client: TestClient, scratch: Scratch):
    (scratch.clone / WATCHLIST).write_text("{}", encoding="utf-8")
    health = client.get("/api/health").json()
    assert health["ok"] is False
    assert health["tree_clean"] is False
    assert "uncommitted" in health["detail"]


def test_health_reports_another_branch(client: TestClient, scratch: Scratch):
    git(scratch.clone, "update-ref", "refs/heads/side", "HEAD")
    git(scratch.clone, "symbolic-ref", "HEAD", "refs/heads/side")
    health = client.get("/api/health").json()
    assert health["ok"] is False
    assert health["branch"] == "side"
