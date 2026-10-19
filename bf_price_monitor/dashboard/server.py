"""Local HTTP layer over the dashboard core library (needs the ``dashboard`` extra).

Run it with ``python -m bf_price_monitor.dashboard.server``. It binds to
127.0.0.1 only and guards against a hostile web page in the user's browser
(design doc, Trap 4, S11): an exact ``Host`` check including the port, a
random per-launch token on every write, JSON bodies only, and no CORS headers.
The token is printed to stdout once and is never logged or echoed (S9).
"""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bf_price_monitor.dashboard.config import (
    ConfigError,
    DashboardConfig,
    load_config,
    validate_clone,
)
from bf_price_monitor.dashboard.gitcmd import Git, GitError, GitTimeout, Runner
from bf_price_monitor.dashboard.gitcmd import default_runner as _default_runner
from bf_price_monitor.dashboard.sync import GitSync

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_HEADER = "x-bf-dashboard-token"
SAFE_METHODS = frozenset({"GET", "HEAD"})
ENV_PREFIX = "BF_DASHBOARD_"

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
    git = Git(cfg.clone, runner)
    try:
        head = git.run("symbolic-ref", "-q", "HEAD", check=False)
        if head.returncode not in (0, 1):
            raise CloneNotReady("cannot read the clone's HEAD")
        ref = head.stdout.strip()
        if head.returncode == 0 and ref != f"refs/heads/{cfg.branch}":
            raise CloneNotReady(
                f"clone is on {ref.removeprefix('refs/heads/')}, "
                f"not {cfg.branch} or a detached HEAD"
            )
        status = git.run("status", "--porcelain", "--untracked-files=no")
    except (GitError, GitTimeout, OSError) as exc:
        raise CloneNotReady(f"cannot inspect the clone: {exc}") from exc
    if status.stdout.strip():
        raise CloneNotReady("clone has uncommitted changes")


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

    hosts = allowed_hosts(port)
    token_bytes = token.encode("utf-8")

    # Middleware added later wraps the earlier ones, so the order of
    # registration below is innermost -> outermost.

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
        # TrustedHostMiddleware (below) checks names but ignores ports, so
        # the exact host:port match happens here.
        if request.headers.get("host", "").lower() not in hosts:
            response: Response = _reject(400, "invalid host")
        else:
            response = await call_next(request)
        for name, value in HARDENING_HEADERS.items():
            response.headers[name] = value
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[LOOPBACK, "localhost"])
    return app


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
        return 2
    token = new_token()
    app = create_app(cfg, token=token, port=args.port, runner=runner)
    # stdout only; the fragment is never sent to the server or to logs.
    print(f"Dashboard: http://{LOOPBACK}:{args.port}/#token={token}", flush=True)
    uvicorn.run(
        app, host=LOOPBACK, port=args.port, log_level="warning", access_log=False
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
