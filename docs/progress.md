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
