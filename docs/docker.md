# Docker mode (T-38, #38)

Docker is an **optional** always-on mode for a home server or VPS. The
default deployment stays GitHub Actions on the self-hosted runner
(`monitor.yml`), which costs nothing and doesn't use this image.

## Quick start (clean machine)

```bash
git clone https://github.com/NaviAndrei/bf-price-monitor.git
cd bf-price-monitor
cp .env.example .env        # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ...
docker compose up -d --build
docker compose logs -f monitor
```

The `monitor` service runs `scripts/run_loop.py run`. Every
`MONITOR_INTERVAL_MINUTES` minutes (120 by default, the same cadence as the
`monitor.yml` cron) it runs `scrape.py`, then `analyze.py`, then `notify.py`,
each as its own process. These are the same scripts the GitHub Actions
workflow runs. A failed stage ends that cycle early. If the Telegram
credentials are missing, `notify` is skipped and logged instead of crashing.

To check a host without scraping anything, run the offline readiness check
(writable volume, SQLite migration, watchlist schema, and a Chromium launch):

```bash
docker compose run --rm monitor selfcheck
```

## Deployment modes and one-consumer rule

| | GitHub Actions (default) | Docker |
|---|---|---|
| Schedule | cron in `monitor.yml` (fires irregularly) | `run_loop.py`, fixed interval |
| State | runtime-state snapshots (#42) | named volume `monitor-data` |
| Feedback buttons (#37) | collected on the next scheduled run | `--profile feedback` worker, within seconds |

Run **one** mode per Telegram bot token. If both run, they alert twice, and
their feedback collectors conflict (Telegram answers `getUpdates` with HTTP
409). To collect feedback presses immediately in Docker mode:

```bash
docker compose --profile feedback up -d
```

Set `TELEGRAM_FEEDBACK_ALLOWED_USER_IDS` only on the host that runs the
worker. See [feedback-labels.md](feedback-labels.md).

## What the image guarantees

The `Docker Image` workflow checks each of these on a GitHub-hosted runner on
every change to the image inputs.

| Property | How |
|---|---|
| Locked dependencies | `uv sync --frozen --no-dev --no-editable` from `uv.lock`; CI compares every installed distribution with `uv.lock` |
| No dev or optional extras | No `--extra` in the build; CI rejects pytest, ruff, mypy, pre-commit, scikit-learn and ruptures |
| Chromium only | `playwright install --with-deps --only-shell chromium`, the headless shell that `chromium.launch(headless=True)` uses; CI rejects Firefox, WebKit and full Chromium |
| Non-root | `USER 10001:10001`; CI checks `id -u` and `id -g` |
| Correct PID 1 | `tini` forwards signals and reaps zombie Chromium processes |
| Graceful shutdown | SIGTERM stops scheduling, forwards SIGTERM to the running stage, and exits 0; `stop_grace_period: 90s`; CI checks exit code 0 in under 90 seconds |
| Healthcheck | `run_loop.py healthcheck`: the loop heartbeat in `/tmp` is refreshed every 30 seconds, even during a long scrape, and is stale after 5 minutes |
| Restart policy | `restart: unless-stopped` |
| Read-only root | `read_only: true`; writes go to the `monitor-data` volume and a 512 MB `/tmp` tmpfs (the Chromium profile, `HOME=/tmp`) |
| Least privilege | `cap_drop: [ALL]`, `no-new-privileges:true` |
| Nothing sensitive baked in | `.dockerignore` is an allowlist (`pyproject.toml`, `uv.lock`, `bf_price_monitor/`, `scripts/*.py`, `data/watchlist.json`); CI checks that `/app/data` holds only `watchlist.json` and that no `.env`, `*.db`, `*.jsonl` or storage-state file exists |
| Pinned bases | `python:3.12-slim-bookworm` and `ghcr.io/astral-sh/uv` pinned by digest; Dependabot (`docker` ecosystem) proposes bumps |

Secrets come only from `.env` at runtime, through `env_file`. `.env` is both
git-ignored and excluded from the build context.

## Resources

The Compose file sets these limits:
- `monitor`: 1.5 CPUs, 2 GB memory, and `shm_size: 1gb`, because Chromium
  crashes with Docker's 64 MB default `/dev/shm`.
- `feedback` worker: 0.25 CPUs, 256 MB.

A cycle scrapes the watchlist in one headless Chromium. On a 2-core, 4 GB
host, leave at least 1 GB for the operating system. Lower the limits in a
`docker-compose.override.yml` if you need to, not in the committed file.

## Data and backups

Everything stateful lives in the named volume `monitor-data`, mounted at
`/app/data`:
- `price_history.db` (SQLite in WAL mode, including feedback labels);
- `price_history.json`;
- `alert_outbox.jsonl`;
- `scrape_health.jsonl`;
- the extraction-failure log.

On first start, the volume is seeded with the image's `watchlist.json`.
After that, edit the watchlist inside the volume, for example with
`docker compose cp`.

```bash
# consistent SQLite backup while running (backup API, not a file copy)
docker compose exec monitor python -c "import sqlite3; s=sqlite3.connect('data/price_history.db'); d=sqlite3.connect('/tmp/backup.db'); s.backup(d); d.close()"
docker compose cp monitor:/tmp/backup.db ./price_history-backup.db
```

`docker compose down` keeps the volume. `docker compose down -v` **deletes it,
along with all history and labels**.

## Security notes

- Playwright runs Chromium without its sandbox by default (the
  `chromiumSandbox` launch option is off). Playwright's Docker guide says a
  seccomp profile is needed to run Chromium *with* the sandbox. Here,
  isolation comes from the container instead: non-root, all capabilities
  dropped, `no-new-privileges`, and a read-only root. The only sites visited
  are the three configured retailers.
- `--ipc=host`, which Playwright suggests for Chromium memory, is deliberately
  not used because it shares the host's IPC namespace. `shm_size` fixes the
  same memory problem.
- Ollama at `localhost:11434` isn't reachable from inside the container.
  Without `HF_TOKEN`, `analyze.py` behaves exactly as when both providers
  fail on the runner.

## Local development limitation

On the Windows development machine, Docker Desktop's engine wasn't running
when this was built. The image has therefore never been built or run locally.
The GitHub-hosted `Docker Image` workflow is the clean-machine acceptance
test of record.
