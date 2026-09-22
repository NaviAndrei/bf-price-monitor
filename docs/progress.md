# Progress Log

Handoff log for completed tasks. Each entry: issue closed, tests status, commit SHA.

## Sprint 0 (T-01..T-04): package, domain models, CI gate, watchlist validation
Tests: 52 passed, 100% coverage on bf_price_monitor/, ruff + mypy strict clean. Commits: 7496b68, f18eb4b, 9f1d44f, 70c69d1, d53452f.

## Sprint 0 reconciliation: merge and push to origin/master
Replaced `-e .` with `.` in requirements.txt for a non-editable install on the monitor.yml runner (a0f9246). Merged origin/master's automated price-history commits with no conflicts (cf2594a) and pushed. Post-merge: 52 tests passed, ruff clean, mypy --strict clean on bf_price_monitor/ (mypy --strict across the whole repo still reports 108 pre-existing errors in scripts/ and tests/, unrelated to Sprint 0 and out of scope). Verified bf_price_monitor/, pyproject.toml, uv.lock, and .github/workflows/quality.yml are present on origin/master. Pushed commit: cf2594a.

## T-09 (#15): per-store health metrics and zero-match regression alerting
Added scrape_health.jsonl per-store health records, Critical Selector Drift quarantine + Telegram alert (via new scrape_health_alerts.json handoff read by notify.py), and a 24h dead-man check.
Tests: 103 passed (96 existing unchanged + 7 new), ruff check and ruff format clean on changed files. Commit: 0c643d4.

## T-11 (#20): bounded notifier retries + dead-letter queue
Replaced the unbounded 429-only retry loop in notify.py with `_send_with_retry`: max 4 attempts, capped exponential backoff + jitter, 429/5xx/network errors retried, permanent 4xx dead-lettered immediately to data/dlq.jsonl. sendPhoto stays a single best-effort attempt that falls back to the retried sendMessage path.
Tests: 111 passed (103 existing unchanged + 8 new), ruff check/format and mypy --strict on bf_price_monitor/ clean. DLQ file confirmed not written by the test suite.

## T-12 (#18): transactional alert outbox with replay-on-restart
Added data/alert_outbox.jsonl: a durable PENDING record is written before every send attempt (deal and health alerts), with a terminal SENT/DEAD_LETTER record appended after resolution. Append-only, latest-record-wins status; deterministic event IDs (uuid5 of alert URL for deals, SHA-256 content hash for health); `_replay_pending_outbox()` resends PENDING records older than a 5-minute grace window at startup; already-SENT events are skipped on replay and in the normal loop.
Tests: 121 passed (111 existing unchanged + 10 new), ruff check/format and mypy --strict on bf_price_monitor/ clean, no data/ writes during tests. Commit: 448dd06.

## T-13 (#22): alert dedup with per-watch cooldown windows
notify.py now suppresses a deal alert whose `dedup_key` (sha256 of url+price+site, "same offer") matches a SENT outbox record within the watch's cooldown window, logging `COOLDOWN SKIP`; this runs after T-12's exact-event dedup and is time-bounded/re-armable, unlike T-12's permanent already-SENT check. `cooldown_hours` is read per-site from data/watchlist.json (min across matching entries, default 24h); watchlist_schema.json's legacy item definition now allows the field. Old outbox records with no `dedup_key` can never suppress.
Tests: 128 passed (121 existing unchanged + 7 new), ruff check/format and mypy --strict on bf_price_monitor/ clean, no data/ writes during tests.

## T-17 (#25): secret scanning, rotation runbook, log scrubbing
Added .gitleaks.toml (extends default ruleset + custom Healthchecks.io ping-URL rule) and .pre-commit-config.yaml (gitleaks v8.30.1), plus a matching "Secret scan (pre-commit / Gitleaks)" step in quality.yml so local and CI share one config. Added docs/security/secret-rotation-runbook.md covering HF_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HEALTHCHECK_URL individually. Audited scrape.py, analyze.py, notify.py for secret-leaking logs: only notify.py's `_send_with_retry` had a real leak path (a network exception's stringified URL can embed the Telegram bot token), now redacted via `_redact_secrets` before it reaches stderr or data/dlq.jsonl. scrape.py and analyze.py needed no changes.
Tests: 128 passed unchanged, ruff check/format clean, Gitleaks scan of full repo clean (no `.gitleaksignore` needed). Commit: e7899a7 (not pushed).

# bf-price-monitor — Current Handoff

## Sprint 3 (Security P0, due 2026-09-27) — 5/6 code-complete
- [x] T-14 (#21) split read/write permissions — landed via earlier commit
- [x] T-15 (#23) SHA-pin actions + lockfile installs — landed via earlier commit
- [x] T-17 (#25) secret scanning/rotation/log redaction — pushed as 04254cf/505e043/8a1b5fd, issue closed, Quality Gate run 35717633185 passed
- [ ] T-16 (#26) harden self-hosted runner
- [x] T-18 (#24) immutable runner environment — pushed as ae4a012, issue closed, verified end-to-end on the real runner (see entry below)
- [x] T-35 (#46) runner outage runbook — see entry below

## 2026-09-22 Price Monitor outage — closed loop
Two independent, previously-conflated failures, both now resolved:
1. **setup-uv libuv crash** — 6 consecutive scheduled runs (2026-09-21T07:18Z
   through 2026-09-22T06:54:44Z) crashed in astral-sh/setup-uv with a libuv
   assertion (`!(handle->flags & UV_HANDLE_CLOSING)`). Fixed by Dependabot
   PR #53, bumping setup-uv to v10.1.0, merged 10:04Z. Confirmed fixed by a
   manual `workflow_dispatch` run (35718553433) where the setup-uv step
   passed cleanly.
2. **DNS resolution failure fetching Playwright from files.pythonhosted.org**
   — surfaced one step later in that same manual run, a distinct failure
   from #1. 10/10 nslookup attempts against the failing domain and a control
   domain both succeeded when tested directly, so classified as transient/
   rare rather than reproducible. Mitigated by raising `UV_HTTP_RETRIES` to
   `"8"` and `UV_HTTP_TIMEOUT` to `"180"`, scoped to the "Install
   dependencies" step only (commit edee65d, merged as 1086639, pushed
   2026-09-22T14:46Z). Quality Gate run 35742703488 on that merge passed.

Pipeline confirmed alive independent of the DNS mitigation: scheduled run
35729797194 (2026-09-22T12:52:05Z, after the setup-uv fix but before the
retry-budget push) completed successfully in 2m2s, including a fresh
`data/price_history.json` commit (46d27e6).

## T-18 (#24): cached/immutable runner environment
Added lockfile-fingerprinted caching to monitor.yml's scrape-analyze-notify job: `UV_PROJECT_ENVIRONMENT` points uv's venv at a runner-local path outside the job workspace (`C:\actions-runner\cache\bf-monitor\venv`), keyed against a SHA-256 hash of uv.lock stored in a sibling `env.marker` file. A hit (marker present, matches fingerprint, venv directory exists) skips `Install dependencies` and `Install Playwright browser` entirely and every downstream `uv run` call uses `--no-sync`; any missing/mismatched signal falls back to a full rebuild. `PLAYWRIGHT_BROWSERS_PATH` was left unchanged. No new `uses:` lines; all Action refs remain full 40-char SHAs; T-14's read/write job split untouched.
Verified end-to-end on the real self-hosted runner, not just simulated: cold run 35775660052 built the venv and browser and logged `runner cache: miss (fingerprint 0bb10822f4ff, setup 10.6s)`; warm run 35776218557 skipped both install steps (confirmed `skipped` conclusion via `gh run view --json jobs`) and logged `runner cache: hit (fingerprint 0bb10822f4ff, setup 2.5s)` with no network/package downloads. Both runs' persist jobs committed and pushed price data successfully.
Tests: 128 passed unchanged, ruff check clean. Commit: ae4a012. Issue #24 closed.

## T-35 (#46): runner outage contingency runbook
Added `docs/runbooks/RUNNER_OUTAGE.md` (trigger criteria, diagnostic checklist, local fallback execution, temporary cloud runner failover, recovery and rollback) and `scripts/run_emergency_local.ps1` (preflight-checks Python 3.11+ and uv, verifies TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set without echoing values, warns-only on missing HF_TOKEN, then runs `uv sync --frozen` → Playwright Chromium install → scrape.py → analyze.py → notify.py with a distinct exit code per failure point). Deliberately out of scope: the issue's proxy/cloud-burst scraping blueprint.
Verified via `-Help` and `-DryRun` (both the missing-secrets and present-secrets paths); not yet exercised as a full live run with real secrets on a non-runner machine — that rehearsal is still pending before #46 can be closed.
Tests: 128 passed unchanged, ruff check clean. Committed together with this progress.md entry (see `git log docs/runbooks/RUNNER_OUTAGE.md` for the SHA).

## Blocked / gating
- T-31 (P0) Black Friday go/no-go review — blocked on Sprint 3-5 completion

## Last session action items
- New issue #54 filed (unrelated tech debt): Quality Gate runs ruff check but not ruff format --check
- Watch item: PC-A1208's VPN adapter route-metric anomaly (see docs/DECISIONS.md) — not fixed, revisit only if DNS errors recur

## Sprints completed
Sprint 0 (foundations), Sprint 1 (correctness), Sprint 2 (notification reliability) — all closed, no action needed.