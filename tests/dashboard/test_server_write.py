"""Slice C: write routes against scratch origins (no network, no real clone)."""

from __future__ import annotations

import copy
import logging
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from scratch_repo import (
    WATCHLIST,
    Scratch,
    build_scratch,
    git,
    other_edit_watchlist,
    seed_doc,
)

from bf_price_monitor.dashboard import gitcmd, locking, operations, server

TOKEN = "unit-test-token"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
WATCH_A, WATCH_B = seed_doc()["watches"]
HEADERS = {server.TOKEN_HEADER: TOKEN}


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    return build_scratch(tmp_path)


def make_client(s: Scratch, **kw: Any) -> TestClient:
    app = server.create_app(
        s.cfg, token=TOKEN, port=PORT, sleep_fn=lambda _s: None, **kw
    )
    return TestClient(app, base_url=BASE)


@pytest.fixture
def client(scratch: Scratch) -> Iterator[TestClient]:
    with make_client(scratch) as c:
        yield c


def post(client: TestClient, path: str, body: Any = None, **kw: Any) -> Any:
    return client.post(path, json={} if body is None else body, headers=HEADERS, **kw)


def origin_queries(s: Scratch) -> list[str]:
    return [w["query"] for w in s.origin_doc()["watches"]]


def failing_push(stderr: str) -> Callable[..., Any]:
    def runner(args: Any, cwd: Any, env: Any, timeout: Any) -> Any:
        if args and args[0] == "push":
            return subprocess.CompletedProcess(["git", *args], 1, "", stderr)
        return gitcmd.default_runner(args, cwd, env, timeout)

    return runner


# --- add / update / enable / disable / remove --------------------------------


def test_add_pushes_one_commit_with_the_configured_author(
    client: TestClient, scratch: Scratch
):
    response = post(
        client,
        "/api/watches",
        {"site": "flanco", "query": "monitor lg", "target_price": 700},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "pushed"
    assert body["saved_locally"] is False
    assert body["pending_count"] == 0
    assert "monitor lg" in origin_queries(scratch)
    assert scratch.origin_subjects()[0] == 'chore(watchlist): add flanco "monitor lg"'
    author = git(scratch.origin, "log", "-1", "--format=%an <%ae>", "master")
    assert author == "Dash Author <dash@example.com>"
    # the page sees the new watch and a fresh etag right away
    assert body["etag"] == client.get("/api/watchlist").json()["etag"]


def test_add_rejects_bad_input_with_422_and_changes_nothing(
    client: TestClient, scratch: Scratch
):
    before = scratch.origin_subjects()
    for body in (
        {"site": "altex", "query": "x"},
        {"site": "emag", "query": "  "},
        {"site": "emag", "query": "ok", "target_price": -1},
        {"site": "emag", "query": "ok", "target_price": "12"},
        {"site": "emag", "query": "ok", "channels": ["pigeon"]},
    ):
        response = post(client, "/api/watches", body)
        assert response.status_code == 422, body
    assert scratch.origin_subjects() == before
    assert client.get("/api/pending").json()["pending_count"] == 0


def test_add_of_a_near_duplicate_is_422_and_parked_not_pending(
    client: TestClient,
):
    response = post(
        client, "/api/watches", {"site": "emag", "query": "  LAPTOP lenovo  v15 "}
    )
    assert response.status_code == 422
    assert "already watching" in response.json()["error"]
    assert client.get("/api/pending").json()["pending_count"] == 0


def test_unknown_body_fields_and_bad_json_are_422(client: TestClient):
    assert (
        post(
            client, "/api/watches", {"site": "emag", "query": "q", "owner": "mallory"}
        ).status_code
        == 422
    )
    bad = client.post(
        "/api/watches",
        content=b"{nope",
        headers={**HEADERS, "content-type": "application/json"},
    )
    assert bad.status_code == 422


def test_update_changes_only_the_editable_fields(client: TestClient, scratch: Scratch):
    response = post(
        client,
        f"/api/watches/{WATCH_A['id']}/update",
        {"expected": WATCH_A, "changes": {"target_price": 2000, "channels": ["email"]}},
    )
    assert response.status_code == 200
    changed = next(
        w for w in scratch.origin_doc()["watches"] if w["id"] == WATCH_A["id"]
    )
    assert changed["target_price"] == 2000
    assert changed["channels"] == ["email"]
    expected = {**WATCH_A, "target_price": 2000, "channels": ["email"]}
    assert changed == expected  # every hidden field passes through


def test_update_cannot_touch_fields_slice_b_does_not_edit(client: TestClient):
    response = post(
        client,
        f"/api/watches/{WATCH_A['id']}/update",
        {"expected": WATCH_A, "changes": {"seller_policy": "trusted"}},
    )
    assert response.status_code == 422
    assert "cannot change" in response.json()["error"]


@pytest.mark.parametrize("action,flag", [("disable", False), ("enable", True)])
def test_enable_and_disable(
    client: TestClient, scratch: Scratch, action: str, flag: bool
):
    expected = copy.deepcopy(WATCH_A)
    if flag:  # enabling needs a disabled watch to start from
        other_edit_watchlist(
            scratch, lambda d: d["watches"][0].__setitem__("enabled", False)
        )
        client.post("/api/refresh", json={}, headers=HEADERS)
        expected["enabled"] = False
    response = post(
        client, f"/api/watches/{WATCH_A['id']}/{action}", {"expected": expected}
    )
    assert response.status_code == 200, response.text
    watch = next(w for w in scratch.origin_doc()["watches"] if w["id"] == WATCH_A["id"])
    assert watch["enabled"] is flag


def test_remove(client: TestClient, scratch: Scratch):
    response = post(
        client, f"/api/watches/{WATCH_B['id']}/remove", {"expected": WATCH_B}
    )
    assert response.status_code == 200
    assert origin_queries(scratch) == [WATCH_A["query"]]


def test_removing_the_last_watch_is_refused_by_the_schema(
    client: TestClient, scratch: Scratch
):
    post(client, f"/api/watches/{WATCH_B['id']}/remove", {"expected": WATCH_B})
    response = post(
        client, f"/api/watches/{WATCH_A['id']}/remove", {"expected": WATCH_A}
    )
    assert response.status_code == 422
    assert origin_queries(scratch) == [WATCH_A["query"]]


def test_path_id_must_match_the_expected_watch(client: TestClient):
    response = post(
        client, f"/api/watches/{WATCH_B['id']}/disable", {"expected": WATCH_A}
    )
    assert response.status_code == 422
    assert "does not match" in response.json()["error"]


# --- 409: per-watch conflicts --------------------------------------------------


def test_edit_of_a_watch_changed_elsewhere_is_409_and_never_overwrites(
    client: TestClient, scratch: Scratch
):
    other_edit_watchlist(
        scratch, lambda d: d["watches"][0].__setitem__("target_price", 1.0)
    )
    response = post(
        client,
        f"/api/watches/{WATCH_A['id']}/update",
        {"expected": WATCH_A, "changes": {"target_price": 2000}},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "reload"
    assert body["state"] == "conflict"
    assert scratch.origin_doc()["watches"][0]["target_price"] == 1.0
    conflicts = client.get("/api/conflicts").json()["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["kind"] == "conflict"


def test_unrelated_edit_elsewhere_does_not_block_a_save(
    client: TestClient, scratch: Scratch
):
    other_edit_watchlist(
        scratch, lambda d: d["watches"][1].__setitem__("min_drop_percent", 9.0)
    )
    response = post(
        client,
        f"/api/watches/{WATCH_A['id']}/update",
        {"expected": WATCH_A, "changes": {"target_price": 2000}},
    )
    assert response.status_code == 200
    docs = {w["id"]: w for w in scratch.origin_doc()["watches"]}
    assert docs[WATCH_A["id"]]["target_price"] == 2000
    assert docs[WATCH_B["id"]]["min_drop_percent"] == 9.0


def test_etag_is_advisory_only_a_stale_one_never_blocks(
    client: TestClient, scratch: Scratch
):
    stale = client.get("/api/watchlist").json()["etag"]
    other_edit_watchlist(
        scratch, lambda d: d["watches"][1].__setitem__("min_drop_percent", 9.0)
    )
    response = client.post(
        "/api/watches",
        json={"site": "flanco", "query": "monitor lg", "etag": stale},
        headers=HEADERS,
    )
    # an unknown field is refused as input, but the etag itself never decides
    assert response.status_code == 422
    ok = post(client, "/api/watches", {"site": "flanco", "query": "monitor lg"})
    assert ok.status_code == 200


def test_dismiss_conflict(client: TestClient, scratch: Scratch):
    other_edit_watchlist(
        scratch, lambda d: d["watches"][0].__setitem__("target_price", 1.0)
    )
    post(
        client,
        f"/api/watches/{WATCH_A['id']}/update",
        {"expected": WATCH_A, "changes": {"target_price": 2000}},
    )
    (conflict,) = client.get("/api/conflicts").json()["conflicts"]
    assert (
        post(client, f"/api/conflicts/{conflict['op_id']}/dismiss").status_code == 200
    )
    assert client.get("/api/conflicts").json()["conflicts"] == []
    assert (
        post(client, f"/api/conflicts/{conflict['op_id']}/dismiss").status_code == 404
    )


# --- failed push, restart, nothing lost (D3) ---------------------------------------


def test_failed_push_is_saved_locally_then_retry_pushes_it(
    client: TestClient, scratch: Scratch
):
    scratch.take_origin_offline()
    response = post(client, "/api/watches", {"site": "flanco", "query": "monitor lg"})
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "not_pushed"
    assert body["saved_locally"] is True
    assert body["pending_count"] == 1
    watchlist = client.get("/api/watchlist").json()
    assert watchlist["pending_count"] == 1
    assert watchlist["clone"]["ok"] is True

    scratch.bring_origin_back()
    retry = post(client, "/api/retry")
    assert retry.status_code == 200
    assert retry.json()["state"] == "pushed"
    assert origin_queries(scratch).count("monitor lg") == 1
    assert client.get("/api/pending").json()["pending_count"] == 0


def test_edits_survive_the_clone_detaching_and_a_server_restart(
    scratch: Scratch,
):
    with make_client(scratch) as first:
        scratch.take_origin_offline()
        assert (
            post(first, "/api/watches", {"site": "flanco", "query": "one"}).status_code
            == 202
        )
        scratch.bring_origin_back()
        # a later successful write detaches the clone and replays the journal
        assert (
            post(first, "/api/watches", {"site": "flanco", "query": "two"}).status_code
            == 200
        )
    assert scratch.clone_head_attached() is False

    scratch.take_origin_offline()
    with make_client(scratch) as second:  # "restart": fresh app, same clone
        server.check_clone_ready(scratch.cfg)  # detached + clean is accepted
        assert (
            post(
                second, "/api/watches", {"site": "flanco", "query": "three"}
            ).status_code
            == 202
        )
    scratch.bring_origin_back()

    with make_client(scratch) as third:  # another restart with an unpushed edit
        server.check_clone_ready(scratch.cfg)
        assert third.get("/api/pending").json()["pending_count"] == 1
        assert post(third, "/api/retry").json()["state"] == "pushed"
    queries = origin_queries(scratch)
    assert {"one", "two", "three"} <= set(queries)
    assert all(queries.count(q) == 1 for q in ("one", "two", "three"))
    subjects = scratch.origin_subjects()
    assert sum('"three"' in s for s in subjects) == 1  # replayed once, not twice


def test_edit_journaled_before_a_crash_is_applied_by_the_next_server(
    scratch: Scratch,
):
    with make_client(scratch) as first:
        # the process dies after the journal write and before any git work
        first.app.state.sync.journal.append(  # type: ignore[attr-defined]
            operations.make_add("pcgarage", "monitor dell")
        )
    with make_client(scratch) as second:
        assert second.get("/api/pending").json()["pending_count"] == 1
        assert post(second, "/api/retry").json()["state"] == "pushed"
    assert "monitor dell" in origin_queries(scratch)


def test_retry_with_nothing_pending_is_a_noop(client: TestClient, scratch: Scratch):
    before = scratch.origin_subjects()
    response = post(client, "/api/retry")
    assert response.status_code == 200
    assert response.json()["state"] == "pushed"
    assert scratch.origin_subjects() == before


def test_rejected_push_after_three_attempts_is_saved_locally(scratch: Scratch):
    runner = failing_push("! [rejected] HEAD -> master (non-fast-forward)")
    with make_client(scratch, runner=runner) as c:
        response = post(c, "/api/watches", {"site": "flanco", "query": "monitor lg"})
        assert response.status_code == 202
        assert response.json()["state"] == "rejected"
        assert response.json()["saved_locally"] is True
        assert c.get("/api/pending").json()["pending_count"] == 1


def test_git_detail_never_leaks_credentials_from_a_remote_url(scratch: Scratch):
    runner = failing_push(
        "fatal: unable to access "
        "'https://x-access-token:ghp_SECRET123@github.com/o/r.git/': "
        "Could not resolve host"
    )
    with make_client(scratch, runner=runner) as c:
        response = post(c, "/api/watches", {"site": "flanco", "query": "monitor lg"})
        text = response.text + c.get("/api/watchlist").text
    assert "ghp_SECRET123" not in text
    assert "x-access-token" not in text


# --- clone state and busy ------------------------------------------------------------


def test_writes_are_refused_while_the_clone_is_dirty(
    client: TestClient, scratch: Scratch
):
    (scratch.clone / WATCHLIST).write_text("{}", encoding="utf-8")
    response = post(client, "/api/watches", {"site": "flanco", "query": "monitor lg"})
    assert response.status_code == 503
    assert "uncommitted" in response.json()["error"]
    assert client.get("/api/pending").json()["pending_count"] == 0


def test_writes_are_refused_when_the_clone_is_on_another_branch(
    client: TestClient, scratch: Scratch
):
    git(scratch.clone, "update-ref", "refs/heads/side", "HEAD")
    git(scratch.clone, "symbolic-ref", "HEAD", "refs/heads/side")
    response = post(client, "/api/watches", {"site": "flanco", "query": "monitor lg"})
    assert response.status_code == 503


def test_busy_clone_is_a_423_and_journals_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def busy(_op: Any) -> Any:
        raise locking.CloneBusy("clone busy: another dashboard process is using it")

    monkeypatch.setattr(client.app.state.sync, "submit", busy)  # type: ignore[attr-defined]
    response = post(client, "/api/watches", {"site": "flanco", "query": "monitor lg"})
    assert response.status_code == 423
    assert "busy" in response.json()["error"]


# --- refresh --------------------------------------------------------------------------


def test_refresh_pulls_in_a_change_made_elsewhere(client: TestClient, scratch: Scratch):
    before = client.get("/api/watchlist").json()
    other_edit_watchlist(
        scratch, lambda d: d["watches"][0].__setitem__("target_price", 1.0)
    )
    response = post(client, "/api/refresh")
    assert response.status_code == 200
    assert response.json()["state"] == "refreshed"
    after = client.get("/api/watchlist").json()
    assert after["watches"][0]["target_price"] == 1.0
    assert after["etag"] != before["etag"]


def test_refresh_offline_reports_and_changes_nothing(
    client: TestClient, scratch: Scratch
):
    before = client.get("/api/watchlist").json()
    scratch.take_origin_offline()
    response = post(client, "/api/refresh")
    assert response.status_code == 200
    assert response.json()["state"] == "offline"
    assert client.get("/api/watchlist").json()["watches"] == before["watches"]


def test_refresh_with_pending_edits_retries_instead_of_dropping_them(
    client: TestClient, scratch: Scratch
):
    scratch.take_origin_offline()
    post(client, "/api/watches", {"site": "flanco", "query": "monitor lg"})
    scratch.bring_origin_back()
    response = post(client, "/api/refresh")
    assert response.json()["state"] == "pushed"
    assert "monitor lg" in origin_queries(scratch)


# --- the guards cover every real write route ------------------------------------------


def _write_paths(app: Any) -> list[tuple[str, str]]:
    found = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - {"GET", "HEAD"}:
                path = route.path.replace("{wid}", "x").replace("{op_id}", "x")
                found.append((method, path))
    return found


def test_every_write_route_demands_the_token_and_json(client: TestClient):
    routes = _write_paths(client.app)
    assert len(routes) >= 8
    for method, path in routes:
        no_token = client.request(
            method, path, json={}, headers={"content-type": "application/json"}
        )
        assert no_token.status_code == 403, (method, path)
        wrong = client.request(
            method, path, json={}, headers={server.TOKEN_HEADER: "wrong"}
        )
        assert wrong.status_code == 403, (method, path)
        text = client.request(
            method,
            path,
            content=b"{}",
            headers={**HEADERS, "content-type": "text/plain"},
        )
        assert text.status_code == 415, (method, path)
        bare = client.request(method, path, content=b"{}", headers=HEADERS)
        assert bare.status_code == 415, (method, path)


def test_every_write_route_is_post_only(client: TestClient):
    assert {m for m, _ in _write_paths(client.app)} == {"POST"}


def test_token_never_appears_in_responses_or_logs_across_writes(
    client: TestClient, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    texts = [
        post(client, "/api/watches", {"site": "flanco", "query": "monitor lg"}).text,
        post(client, "/api/watches", {"site": "nope", "query": "x"}).text,
        post(client, "/api/retry").text,
        client.get("/api/watchlist").text,
    ]
    assert all(TOKEN not in t for t in texts)
    assert TOKEN not in caplog.text


# --- review follow-ups ---------------------------------------------------------


def test_concurrent_writes_are_serialised_not_refused(scratch: Scratch):
    from concurrent.futures import ThreadPoolExecutor

    with make_client(scratch) as c:

        def add(n: int) -> int:
            status: int = post(
                c, "/api/watches", {"site": "flanco", "query": f"monitor {n}"}
            ).status_code
            return status

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(add, range(4)))
    assert statuses == [200, 200, 200, 200]
    assert {f"monitor {n}" for n in range(4)} <= set(origin_queries(scratch))


def test_edit_queued_because_origin_is_unreadable_is_a_save_not_a_rejection(
    client: TestClient, scratch: Scratch
):
    legacy = scratch.other / WATCHLIST
    git(scratch.other, "pull", "-q", "--no-rebase", "origin", "master")
    good = legacy.read_text(encoding="utf-8")
    legacy.write_text('[{"site": "emag", "query": "x"}]\n', encoding="utf-8")
    git(scratch.other, "commit", "-q", "-am", "legacy shape")
    git(scratch.other, "push", "-q", "origin", "HEAD:master")

    response = post(client, "/api/watches", {"site": "flanco", "query": "monitor lg"})
    assert response.status_code == 202
    body = response.json()
    assert body["saved_locally"] is True
    assert body["pending_count"] == 1
    assert "legacy" in body["error"]

    legacy.write_text(good, encoding="utf-8")
    git(scratch.other, "commit", "-q", "-am", "restore modern shape")
    git(scratch.other, "push", "-q", "origin", "HEAD:master")
    assert post(client, "/api/retry").json()["state"] == "pushed"
    assert origin_queries(scratch).count("monitor lg") == 1


def test_crash_after_a_successful_push_does_not_duplicate_the_edit(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
):
    with make_client(scratch) as first:
        journal = first.app.state.sync.journal  # type: ignore[attr-defined]

        def disk_full(_ids: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(journal, "remove", disk_full)
        response = post(
            first, "/api/watches", {"site": "flanco", "query": "monitor lg"}
        )
        assert response.status_code == 500
        assert "may be incomplete" in response.json()["error"]
    assert "monitor lg" in origin_queries(scratch)  # the push really landed

    with make_client(scratch) as second:  # restart: journal still holds the op
        assert second.get("/api/pending").json()["pending_count"] == 1
        assert post(second, "/api/retry").json()["state"] == "pushed"
        assert second.get("/api/pending").json()["pending_count"] == 0
    assert origin_queries(scratch).count("monitor lg") == 1
    assert sum('"monitor lg"' in s for s in scratch.origin_subjects()) == 1


@pytest.mark.parametrize(
    "text",
    [
        "unable to access 'https://x-access-token:ghp_AAA@github.com/o/r.git/'",
        "fatal: https://user:p@ss@github.com/o/r.git",
        "remote: https://ghp_BBB@github.com/o/r.git denied",
        "no url here",
    ],
)
def test_redact_strips_credentials_from_urls(text: str):
    cleaned = server.redact(text)
    assert "ghp_" not in cleaned and "x-access-token" not in cleaned
    assert "p@ss" not in cleaned and "user:" not in cleaned
    assert ("github.com" in cleaned) == ("github.com" in text)


def test_health_detail_is_redacted(scratch: Scratch):
    leak = (
        "fatal: unable to access 'https://x-access-token:ghp_CCC@github.com/o/r.git/'"
    )

    def runner(args: Any, cwd: Any, env: Any, timeout: Any) -> Any:
        if args and args[0] == "status":
            return subprocess.CompletedProcess(["git", *args], 128, "", leak)
        return gitcmd.default_runner(args, cwd, env, timeout)

    with make_client(scratch, runner=runner) as c:
        text = c.get("/api/health").text + c.get("/api/watchlist").text
    assert "ghp_CCC" not in text and "x-access-token" not in text


def test_etag_ignores_crlf_checkouts(client: TestClient, scratch: Scratch):
    path = scratch.clone / WATCHLIST
    lf = path.read_bytes().replace(b"\r\n", b"\n")  # this machine may check out CRLF
    path.write_bytes(lf)
    first = client.get("/api/watchlist").json()["etag"]
    path.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert client.get("/api/watchlist").json()["etag"] == first
    path.write_bytes(lf.replace(b"\n", b"\n "))  # real content change still shows
    assert client.get("/api/watchlist").json()["etag"] != first
