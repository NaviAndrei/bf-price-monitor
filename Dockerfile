# syntax=docker/dockerfile:1
# T-38 (#38): always-on Docker mode. GitHub Actions (monitor.yml) remains the
# zero-cost default and does not use this image. See docs/docker.md.
#
# Base images are pinned by digest (tag kept for readability); bump both
# together. The Python base must be identical in both stages because the
# virtualenv's interpreter symlink points at /usr/local/bin/python3.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

# --- builder: locked dependencies only, from uv.lock -------------------------
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# --frozen: fail instead of re-resolving if uv.lock is stale. No extras, so
# dev tools (and #36's optional anomaly stack) never reach this image.
RUN uv sync --frozen --no-install-project --no-dev
COPY bf_price_monitor ./bf_price_monitor
RUN uv sync --frozen --no-dev --no-editable

# --- runtime ------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/.venv /app/.venv
# Chromium headless shell only (what chromium.launch(headless=True) uses),
# plus its OS libraries. No Firefox/WebKit, no full Chromium.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright
RUN groupadd --gid 10001 monitor \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp \
       --shell /usr/sbin/nologin monitor
WORKDIR /app
COPY scripts ./scripts
# Owned by the runtime user so a fresh named volume mounted here (which
# copies this directory's ownership) is writable without extra capabilities.
RUN install -d -o 10001 -g 10001 -m 0750 /app/data
# Only the watchlist config is baked in; it seeds an empty named volume on
# first start. No SQLite, history, outbox, .env or browser profile ever is
# (see .dockerignore, which is an allowlist).
COPY --chown=10001:10001 data/watchlist.json ./data/watchlist.json
USER 10001:10001
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --start-interval=5s --retries=3 \
    CMD ["python", "scripts/run_loop.py", "healthcheck"]
ENTRYPOINT ["tini", "--", "python", "scripts/run_loop.py"]
CMD ["run"]
