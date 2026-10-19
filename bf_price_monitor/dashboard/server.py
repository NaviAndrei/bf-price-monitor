"""Local HTTP layer over the dashboard core library (needs the ``dashboard`` extra).

Run it with ``python -m bf_price_monitor.dashboard.server``. It binds to
127.0.0.1 only and guards against a hostile web page in the user's browser
(design doc, Trap 4, S11): an exact ``Host`` check including the port, a
random per-launch token on every write, JSON bodies only, and no CORS headers.
The token is printed to stdout once and is never logged or echoed (S9).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bf_price_monitor.dashboard import operations, store
from bf_price_monitor.dashboard.config import (
    ConfigError,
    DashboardConfig,
    load_config,
    validate_clone,
)
from bf_price_monitor.dashboard.gitcmd import (
    NETWORK_TIMEOUT,
    Git,
    GitError,
    GitTimeout,
    Runner,
    classify_failure,
)
from bf_price_monitor.dashboard.gitcmd import default_runner as _default_runner
from bf_price_monitor.dashboard.journal import JournalError
from bf_price_monitor.dashboard.locking import CloneBusy, CloneLock
from bf_price_monitor.dashboard.operations import InvalidOperation
from bf_price_monitor.dashboard.sites import CHANNELS, KNOWN_SITES
from bf_price_monitor.dashboard.store import StoreError
from bf_price_monitor.dashboard.sync import WATCHLIST_REL, GitSync, SyncResult

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_HEADER = "x-bf-dashboard-token"
SAFE_METHODS = frozenset({"GET", "HEAD"})
ENV_PREFIX = "BF_DASHBOARD_"
DETACHED = "detached"

HARDENING_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "content-security-policy": (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'none'"
    ),
}

Next = Callable[[Request], Awaitable[Response]]


class CloneNotReady(Exception):
    """The clone is dirty or on a branch other than the configured one."""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"{LOOPBACK}:{port}", f"localhost:{port}"})


def check_clone_ready(cfg: DashboardConfig, runner: Runner = _default_runner) -> None:
    """Clean, and detached or on the configured branch (D3).

    Slice B detaches the clone before every sync, so "attached to master" is
    not required; attached to anything else is refused.
    """
    branch, clean = _inspect_clone(cfg, runner)
    if branch not in (DETACHED, cfg.branch):
        raise CloneNotReady(
            f"clone is on {branch}, not {cfg.branch} or a detached HEAD"
        )
    if not clean:
        raise CloneNotReady("clone has uncommitted changes")


def _inspect_clone(cfg: DashboardConfig, runner: Runner) -> tuple[str, bool]:
    """(branch name or ``detached``, whether tracked files are unmodified)."""
    git = Git(cfg.clone, runner)
    try:
        head = git.run("symbolic-ref", "-q", "HEAD", check=False)
        if head.returncode not in (0, 1):
            raise CloneNotReady("cannot read the clone's HEAD")
        branch = (
            head.stdout.strip().removeprefix("refs/heads/")
            if head.returncode == 0
            else DETACHED
        )
        status = git.run("status", "--porcelain", "--untracked-files=no")
    except (GitError, GitTimeout, OSError) as exc:
        raise CloneNotReady(f"cannot inspect the clone: {exc}") from exc
    return branch, not status.stdout.strip()


def clone_health(cfg: DashboardConfig, runner: Runner) -> dict[str, Any]:
    """Never raises: the page shows this even when the clone is unusable."""
    try:
        branch, clean = _inspect_clone(cfg, runner)
        head = Git(cfg.clone, runner).run("rev-parse", "--short", "HEAD").stdout
    except (CloneNotReady, GitError, GitTimeout, OSError) as exc:
        return {
            "ok": False,
            "branch": "unknown",
            "tree_clean": False,
            "head": "",
            "detail": redact(str(exc)),
        }
    detail = ""
    if branch not in (DETACHED, cfg.branch):
        detail = f"clone is on {branch}, not {cfg.branch} or a detached HEAD"
    elif not clean:
        detail = "clone has uncommitted changes"
    return {
        "ok": not detail,
        "branch": branch,
        "tree_clean": clean,
        "head": head.strip(),
        "detail": detail,
    }


def _register_read_routes(app: FastAPI) -> None:
    @app.exception_handler(StoreError)
    async def store_error(request: Request, exc: StoreError) -> Response:
        return _reject(503, str(exc))

    @app.exception_handler(JournalError)
    async def journal_error(request: Request, exc: JournalError) -> Response:
        return _reject(500, str(exc))

    def sync_of() -> GitSync:
        sync: GitSync = app.state.sync
        return sync

    def pending_view() -> dict[str, Any]:
        status = sync_of().status()
        return {
            "pending": sync_of().journal.pending(),
            "pending_count": status.pending_count,
            "conflicts": status.conflicts,
        }

    @app.get("/api/watchlist")
    def get_watchlist() -> dict[str, Any]:
        cfg: DashboardConfig = app.state.cfg
        path = cfg.clone / WATCHLIST_REL
        doc = store.load(path)
        return {
            "watches": doc["watches"],
            "etag": _etag(cfg),  # advisory only: writes are never gated on it (D2)
            "sites": list(KNOWN_SITES),
            "channels": list(CHANNELS),
            "clone": clone_health(cfg, app.state.runner),
            **pending_view(),
        }

    @app.get("/api/pending")
    def get_pending() -> dict[str, Any]:
        view = pending_view()
        return {"pending": view["pending"], "pending_count": view["pending_count"]}

    @app.get("/api/conflicts")
    def get_conflicts() -> dict[str, Any]:
        return {"conflicts": sync_of().status().conflicts}

    @app.get("/api/health")
    def get_health() -> dict[str, Any]:
        health: dict[str, Any] = clone_health(app.state.cfg, app.state.runner)
        return health


def _reject(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def create_app(
    cfg: DashboardConfig,
    *,
    token: str,
    port: int,
    runner: Runner = _default_runner,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> FastAPI:
    # No interactive docs: they load assets from a CDN (and expose the schema).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.runner = runner
    app.state.sync = GitSync(cfg, runner=runner, sleep_fn=sleep_fn)
    # One write at a time in this process: the clone-state check and the
    # action it guards must not interleave with another request's edit.
    app.state.write_lock = threading.Lock()

    hosts = allowed_hosts(port)
    token_bytes = token.encode("utf-8")

    # Middleware added later wraps the earlier ones, so the order of
    # registration below is innermost -> outermost: TrustedHostMiddleware
    # (names only, ignores ports), then the token and content-type guard,
    # then the exact host:port guard, which also stamps every response.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[LOOPBACK, "localhost"])

    @app.middleware("http")
    async def write_guard(request: Request, call_next: Next) -> Response:
        if request.method not in SAFE_METHODS:
            supplied = request.headers.get(TOKEN_HEADER, "").encode("utf-8")
            if not hmac.compare_digest(supplied, token_bytes):
                return _reject(403, "missing or invalid token")
            if _media_type(request.headers.get("content-type", "")) != (
                "application/json"
            ):
                return _reject(415, "request body must be application/json")
        return await call_next(request)

    @app.middleware("http")
    async def host_guard(request: Request, call_next: Next) -> Response:
        # TrustedHostMiddleware checks names but ignores ports, so the exact
        # host:port match happens here, before the token is even looked at.
        if request.headers.get("host", "").lower() not in hosts:
            response: Response = _reject(400, "invalid host")
        else:
            response = await call_next(request)
        for name, value in HARDENING_HEADERS.items():
            response.headers[name] = value
        return response

    _register_read_routes(app)
    _register_write_routes(app)
    _register_ui(app)
    return app


_USERINFO = re.compile(r"(?<=://)[^/\s]*@")


def redact(text: str) -> str:
    """Drop ``user:secret@`` from URLs so git's messages are safe to return."""
    return _USERINFO.sub("", text)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddBody(_Body):
    # Any: values are validated (types, ranges) by operations.make_add.
    site: str
    query: str
    target_price: Any = None
    min_drop_percent: Any = None
    channels: Any = None


class EditBody(_Body):
    expected: dict[str, Any]
    changes: dict[str, Any]


class ExpectedBody(_Body):
    expected: dict[str, Any]


def _etag(cfg: DashboardConfig) -> str | None:
    """sha256 of the file with CRLF folded to LF, so a Windows checkout with
    core.autocrlf and the LF bytes the server writes hash the same."""
    try:
        raw = (cfg.clone / WATCHLIST_REL).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _respond(
    result: SyncResult, cfg: DashboardConfig, *, queued: bool = False
) -> JSONResponse:
    body: dict[str, Any] = {
        "state": result.state,
        "reason": result.reason,
        "detail": redact(result.detail),
        "saved_locally": queued or result.state in ("not_pushed", "rejected"),
        "pending_count": result.pending_count,
        "conflicts": result.conflicts,
        "head": result.head,
        "etag": _etag(cfg),
    }
    if result.state == "conflict":
        body["error"] = "reload"
        return JSONResponse(body, status_code=409)
    if result.state == "invalid":
        body["error"] = redact(result.detail)
        return JSONResponse(body, status_code=202 if queued else 422)
    # pushed: done. not_pushed / rejected: kept in the journal, 202 Accepted.
    return JSONResponse(body, status_code=200 if result.state == "pushed" else 202)


def _register_write_routes(app: FastAPI) -> None:
    def sync_of() -> GitSync:
        sync: GitSync = app.state.sync
        return sync

    def guarded(action: Callable[[], JSONResponse]) -> JSONResponse:
        """Clone-state check, then the action; map the failures to HTTP."""
        cfg: DashboardConfig = app.state.cfg
        try:
            with app.state.write_lock:
                check_clone_ready(cfg, app.state.runner)
                return action()
        except CloneNotReady as exc:
            return _reject(503, redact(str(exc)))
        except CloneBusy as exc:
            return _reject(423, str(exc))
        except (GitError, GitTimeout, OSError) as exc:
            # The edit may already be journaled or even pushed: say so.
            return _reject(
                500,
                redact(f"save may be incomplete ({exc}); reload to check"),
            )

    def submit(build: Callable[[], operations.Op]) -> JSONResponse:
        try:
            op = build()
        except InvalidOperation as exc:
            return _reject(422, str(exc))

        def run() -> JSONResponse:
            sync = sync_of()
            before = {e["op_id"] for e in sync.journal.pending()}
            result = sync.submit(op)
            # "invalid" because origin's file is unreadable leaves the new
            # edit queued in the journal: that is a save, not a rejection.
            queued = result.state == "invalid" and any(
                e["op_id"] not in before for e in sync.journal.pending()
            )
            return _respond(result, app.state.cfg, queued=queued)

        return guarded(run)

    def matching(wid: str, expected: dict[str, Any]) -> dict[str, Any]:
        if expected.get("id") != wid:
            raise InvalidOperation(
                "watch id in the path does not match the expected one"
            )
        return expected

    @app.post("/api/watches")
    def add_watch(body: AddBody) -> JSONResponse:
        return submit(
            lambda: operations.make_add(
                body.site,
                body.query,
                target_price=body.target_price,
                min_drop_percent=body.min_drop_percent,
                channels=body.channels,
            )
        )

    @app.post("/api/watches/{wid}/update")
    def update_watch(wid: str, body: EditBody) -> JSONResponse:
        return submit(
            lambda: operations.make_update(matching(wid, body.expected), body.changes)
        )

    @app.post("/api/watches/{wid}/enable")
    def enable_watch(wid: str, body: ExpectedBody) -> JSONResponse:
        return submit(
            lambda: operations.make_set_enabled(matching(wid, body.expected), True)
        )

    @app.post("/api/watches/{wid}/disable")
    def disable_watch(wid: str, body: ExpectedBody) -> JSONResponse:
        return submit(
            lambda: operations.make_set_enabled(matching(wid, body.expected), False)
        )

    @app.post("/api/watches/{wid}/remove")
    def remove_watch(wid: str, body: ExpectedBody) -> JSONResponse:
        return submit(lambda: operations.make_delete(matching(wid, body.expected)))

    @app.post("/api/retry")
    def retry_pending() -> JSONResponse:
        return guarded(lambda: _respond(sync_of().retry(), app.state.cfg))

    @app.post("/api/refresh")
    def refresh() -> JSONResponse:
        cfg: DashboardConfig = app.state.cfg

        def action() -> JSONResponse:
            sync = sync_of()
            if sync.journal.pending():
                # Detaching onto origin would hide unpushed edits; replaying
                # the journal is the safe way to catch up.
                return _respond(sync.retry(), cfg)
            ref = f"refs/remotes/{cfg.remote}/{cfg.branch}"
            git = Git(cfg.clone, app.state.runner)
            with CloneLock(sync.lock_path):
                try:
                    fetch = git.run(
                        "fetch",
                        cfg.remote,
                        f"refs/heads/{cfg.branch}:{ref}",
                        timeout=NETWORK_TIMEOUT,
                        check=False,
                    )
                except GitTimeout as exc:
                    return JSONResponse(
                        {"state": "offline", "reason": "timeout", "detail": str(exc)}
                    )
                if fetch.returncode != 0:
                    last = (fetch.stderr.strip().splitlines() or [""])[-1]
                    return JSONResponse(
                        {
                            "state": "offline",
                            "reason": classify_failure(fetch.stderr),
                            "detail": redact(last),
                        }
                    )
                git.run("switch", "--detach", ref)
            return JSONResponse({"state": "refreshed", "etag": _etag(cfg)})

        return guarded(action)

    @app.post("/api/conflicts/{op_id}/dismiss")
    def dismiss_conflict(op_id: str) -> JSONResponse:
        if sync_of().dismiss_conflict(op_id):
            return JSONResponse({"dismissed": True})
        return _reject(404, "no such conflict")


STATIC_DIR = Path(__file__).parent / "static"


def _register_ui(app: FastAPI) -> None:
    """The page and its two assets, all served from this package (no CDN)."""
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


def _parse_port(value: str) -> int:
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    runner: Runner = _default_runner,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m bf_price_monitor.dashboard.server")
    parser.add_argument("--port", type=_parse_port, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    source = os.environ if env is None else env
    # Only the dashboard's own settings are ever looked at.
    scoped = {k: v for k, v in source.items() if k.startswith(ENV_PREFIX)}
    try:
        cfg = load_config(scoped, runner)
        validate_clone(cfg, runner=runner)
        check_clone_ready(cfg, runner)
    except (ConfigError, CloneNotReady) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "uncommitted" in str(exc):
            # e.g. the previous server died between writing and committing
            print(
                "hint: in the dashboard clone run: "
                "git restore --staged --worktree .   (queued edits are kept "
                "in .git/bf-dashboard and are replayed on the next save)",
                file=sys.stderr,
            )
        return 2
    token = new_token()
    app = create_app(cfg, token=token, port=args.port, runner=runner)
    # stdout only; the fragment is never sent to the server or to logs.
    print(f"Dashboard: http://{LOOPBACK}:{args.port}/#token={token}", flush=True)
    uvicorn.run(
        app,
        host=LOOPBACK,
        port=args.port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,  # never trust X-Forwarded-* from a local client
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
