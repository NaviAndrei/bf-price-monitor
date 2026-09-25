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

## T-41 (#58): dedup id, outbox and cooldown bugs found during T-40 investigation
Fixed four notify.py bugs surfaced while scoping T-40's fan-out: outbox event id was URL-only (a genuine price drop was silently skipped), the photo send path never wrote to the outbox, a replayed PENDING record could crash the cooldown check for lacking a timestamp, and `cooldown_hours` had been dropped from the Watch model. Restored `cooldown_hours` (default 24) on Watch and the modern schema, carried over by migrate_watchlist_to_modern.py; data/watchlist.json left unchanged since every fallback already resolves to 24.
Tests: 150 passed (139 existing + 11 new), ruff check/format clean (pre-existing scripts/analyze.py format issue unrelated, tracked by #54). Commit: 3c1a960.

## Sprint 3 (Security P0, due 2026-09-27) — 6/6 complete
Sprint 3 (Security P0) fully closed ahead of 2026-09-27 due date.
- [x] T-14 (#21) split read/write permissions — landed via earlier commit
- [x] T-15 (#23) SHA-pin actions + lockfile installs — landed via earlier commit
- [x] T-17 (#25) secret scanning/rotation/log redaction — pushed as 04254cf/505e043/8a1b5fd, issue closed, Quality Gate run 35717633185 passed
- [x] T-16 (#26) harden self-hosted runner — see entry below
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

## T-16 (#26): harden self-hosted runner
Added `scripts/runner_cleanup.ps1` (idempotent, `-DryRun`-capable: terminates orphaned Chromium/chrome processes older than 30 minutes, purges `%TEMP%` debris older than 24 hours, reports without touching the T-18 venv cache) and a post-job "Runner cleanup" step (`if: always()`) in monitor.yml's `scrape-analyze-notify` job. Added `docs/runbooks/RUNNER_SETUP.md`: documents the DACL gap found on inspection (`Authenticated Users` held Modify on both `C:\actions-runner` and its cache subdirectory) with the `icacls` commands to revoke it and re-scope to the runner's own group/SYSTEM/Administrators, least-privilege service account guidance (`NETWORK SERVICE` satisfies non-admin but isn't a dedicated isolated account), ASR/Exploit Protection recommendations, and a full reinstall/disaster-recovery procedure.
Tests: 128 passed unchanged, ruff check clean, monitor.yml YAML validated and all `uses:` refs still full 40-char SHAs. Commit: see `git log scripts/runner_cleanup.ps1` for the SHA.

## T-19 (#28): SQLite storage layer
Added `bf_price_monitor/storage/sqlite.py`: three-table schema (canonical_products,
offers, price_observations) with WAL mode, foreign keys, and idempotent
upserts in `record_observation` (ON CONFLICT on `(retailer, sku)` and
`(offer_id, scraped_at)`). Product/offer IDs derived via `uuid5(NAMESPACE_URL, ...)`
from title and retailer+sku respectively.
Tests: 139 passed (128 existing + 11 new in `tests/unit/test_sqlite_storage.py`).

## T-20 (#27): history migration script (JSON to SQLite) with verification
Added `scripts/migrate_history_to_sqlite.py` (CLI with --source/--target/--dry-run,
per-product exception isolation) and `scripts/verify_migration.py` (independent
verifier — recomputes expected counts from source JSON without importing the
migration script, checks PRAGMA integrity_check/foreign_key_check, and
sample-verifies 10 random products).
Verified against a disposable test-copy database only: 294 products / 3,150
source observations migrated to 3,148 target rows (delta of 2 is expected and
verified — two eMAG listings were re-slugged mid-monitoring under the same
retailer product code, so they correctly collapse into one offer). Idempotency
proven by running the migration twice against the same test copy with
identical row counts both times (291 products, 292 offers, 3,148 observations).
Tests: 139 passed unchanged, ruff check clean.
**Not yet run against the real `data/price_history.db`** — this commit ships
the scripts only; the production migration run is a separate, explicit
decision.

## T-37 (#48) Phase 2: wire Watch model into watchlist validation
Extended `Watch` with independent optional `target_price`/`min_drop_percent`, `owner` (default "NaviAndrei"), and `seller_policy` (default "any"); `drop_rule`/`drop_threshold` became optional and `track_all_time_low` defaults to `True`, since none of the 4 real legacy watchlist entries populate them and `should_alert()` never reads `drop_rule`/`drop_threshold` in the live alerting path. Mirrored the new fields into `watchlist_schema.json`'s `modernWatchItem`, including the `enabled` boolean that was already a `Watch` default but missing from the schema. Wired `Watch.model_validate()` into `load_watchlist()` at the modern-format boundary; the legacy flat-array branch stays untouched. Added an additive SQLite dual-write in `scrape.py`'s per-result loop. Added `scripts/migrate_watchlist_to_modern.py` to convert the legacy watchlist to the modern shape — two real bugs (Decimal serialized as a JSON string, missing `enabled` schema property) were found and fixed during a promote/validate/revert review cycle before either reached `data/watchlist.json`. A third, more severe gap was then found and **not** fixed in this task: neither `Watch` nor `modernWatchItem` has a `site`/retailer field, so migrated entries lose retailer identity entirely, and `scrape.py`'s `main()` reads `item["site"]` unconditionally — promoting the migration script's output to the live watchlist as currently defined would crash the next scrape run. `data/watchlist.json` was reverted to its committed legacy-format baseline and excluded from this commit; the migration script's docstring now documents this as unsafe to run for real. Follow-up filed as #56 (T-39).
Tests: 139 passed, ruff check clean. Commit: dc4cc6c. Issue #48 closed via `Closes #48` trailer auto-close on push; #56 opened for the deferred site/retailer-identity gap.

## T-39 (#56): site field on Watch, real watchlist promotion
Added `site: str` (required) to Watch and modernWatchItem — all 4 real
watchlist entries always populated it, so required-ness matches legacy.
migrate_watchlist_to_modern.py now carries site over 1:1; its docstring's
"not safe to run for real" warning is removed. Ran the real migration
(scratch-target first, validated via validate_watchlist(), then
promoted): data/watchlist.json is live in modern format with site
intact. Option B (multi-retailer fan-out + seller_policy enforcement)
investigated and deferred to #57 (T-40) rather than attempted here.
Tests: 139 passed, ruff check clean. Commit: f29a282.

## T-40 (#57) Phase 3: seller_policy "trusted" gate on is_marketplace
Implemented Option 1 of the seller_policy semantics options (first-party-verified gate): a "trusted" watch now skips alerting unless the matched listing's `is_marketplace` is `False`. eMAG's listings always report `is_marketplace: None` (robots.txt blocks the only page with real seller text), so a "trusted" watch on eMAG never alerts — a deliberate, visible degradation, surfaced via a new `policy_blocked_count` stat in scrape_health.jsonl rather than silently looking like "no price drop." Option 3 (per-Watch seller allowlist) rejected: no retailer exposes a real per-listing seller to allowlist against today. The fan-out rewrite (multiple retailers per watch) remains out of scope and untouched.
Tests: 153 passed (3 new, TDD — shown failing before the gate was added), ruff check clean. Commit: 56a89fc.

Sprint 4 (Storage Migration to SQLite, due 2026-09-29) closed: #6 (Parent), #28 (T-19), #27 (T-20) all complete.

Watch item carried forward: PC-A1208's VPN adapter route-metric anomaly (see docs/DECISIONS.md) — not fixed, revisit only if DNS errors recur.

## T-37b (#55): wire DeliveryAttempt into SQLite as terminal audit log
Added additive `delivery_attempts` table written from notify.py; JSONL outbox stays sole authority for PENDING/replay/cooldown/dedup. attempt_number allocated atomically at the storage-write boundary (single INSERT...SELECT), closing a read-then-write race caught in review.
Tests: 170 passed, ruff check/format clean (scripts/analyze.py pre-existing drift only). Commit: 8e70052.

## T-21 (#29): reuse one Playwright browser per monitoring run with isolated contexts
Replaced fetch_with_browser's per-fetch full browser launch with a run-scoped _BrowserState: one stealth-wrapped Chromium browser launches lazily on first real use per scripts/scrape.py run, with one reusable BrowserContext per retailer (pcgarage, flanco) isolating cookies/storage so no state crosses sites. A 403/429 retry inside fetch_with_browser reuses the same site context so recoverable challenge/session cookies survive; only a scraper-level retry after an unhandled exception discards and rebuilds that one site's context via with_retry's new optional site_name parameter, leaving other sites and the browser untouched. Each fetch attempt opens and closes its own Page in finally. main() now wraps the run body in try/finally so _browser_state.close() tears down every context, the browser, and the stealth/Playwright context unconditionally, even if the watchlist loop raises.
Tests: 182 passed (170 existing unchanged + 12 new in tests/test_browser_state.py), ruff check clean, ruff format --check clean except scripts/analyze.py's pre-existing drift (#54). Commit: 1b25f7f.
**Live before/after performance measurement on the self-hosted runner remains pending and unapproved** — no workflow_dispatch or real retailer scrape was run as part of this implementation. #29 stays open until that acceptance criterion is explicitly approved and run.

### T-21 live-validation regression and correction
The first approved live workflow_dispatch run against 1b25f7f failed PC Garage and Flanco entirely: fetch_with_browser called context.new_page(user_agent=HEADERS["User-Agent"]), but the real playwright BrowserContext.new_page() takes no arguments, so both retailers raised TypeError on every attempt and produced zero data (scrape_health.jsonl showed products_parsed=0 for both, versus 20/10 on the last good pre-change run). This is unrelated to the separate, still-broken Runner cleanup step, which fails independently because pwsh is missing from the runner's PATH.
Corrective fix: removed the invalid user_agent keyword from context.new_page(), leaving user_agent configured only at browser.new_context() in _BrowserState.get_context, and tightened tests/test_browser_state.py's FakeContext from permissive **kwargs to the real zero-argument new_page() shape, plus a new regression test reproducing the exact production TypeError before the fix.
Tests: 183 passed (13 in tests/test_browser_state.py, including the new regression test), ruff check/format clean except scripts/analyze.py's pre-existing drift. Commit: 5cce44b.

### T-21 (#29): closed — live validation passed
Validation run 36128084436 at HEAD 6915ee8: Scrape prices completed in 40s versus the pre-change 42s baseline (run 36116716480), 4.8% faster, with PC Garage/Flanco parsing the same 20/10 products as baseline and no TypeError, challenge-loop, selector, or browser-lifecycle warnings. Dry-run cleanup found no Chrome process left behind by the run. Browser reuse, isolated retailer contexts, retry-reset behavior, page cleanup, and live retailer compatibility are all validated against a real scrape, not just unit tests. Issue #29 closed.

## T-42 (#60): fix Runner cleanup's pwsh invocation and restore downstream persistence
Diagnosed: `Get-Command pwsh` resolved fine at the outer shell (every other monitor.yml step using an implicit PowerShell `run:` block passed), but the Runner cleanup step's bare `pwsh scripts/runner_cleanup.ps1` was a nested process spawn whose inherited child-process PATH didn't include pwsh's install directory — pwsh installed but not resolvable from that inner lookup. Since `runner_cleanup.ps1` itself uses no PowerShell-7-only syntax, monitor.yml's Runner cleanup step now resolves `pwsh` or falls back to `powershell.exe` explicitly via `Get-Command` and invokes it by resolved path, failing fast with a clear message if neither exists; `scripts/runner_cleanup.ps1` was not modified.
Local dry-run verification resolved pwsh.exe correctly and reported the same 18 pre-existing orphaned processes (all 1,000+ minutes old), nothing under the 30-minute threshold flagged. Commit: bb64639.
Live validation run [36130859283](https://github.com/NaviAndrei/bf-price-monitor/actions/runs/36130859283) at HEAD bb64639: every step in scrape-analyze-notify succeeded including Runner cleanup for the first time since the regression began, overall job conclusion success, and the persist job ran (not skipped) and committed price data as 77bdb72. Issue #60 closed.

## T-22 (#31): resilient waits — implementation complete, held open pending live challenge observation
Commit a85c8c1 replaced the fixed-duration Cloudflare-challenge poll loop in `fetch_with_browser` with a bounded Playwright locator wait (`_wait_out_challenge`, genuine `.wait_for(state=..., timeout=...)` calls instead of `time.sleep()` ticks); all other `time.sleep()` calls in scrape.py (403/429 retry backoff, scraper-level retry backoff, inter-item pacing in `main()`) were left untouched as deliberate out-of-scope throttling, not page-condition waits. Commit ca56204 added a `challenge_wait_entered` health field and `challenge_wait_entered_counts` run-state counter, since `challenge_detected` only fires on final failure and can't distinguish "no challenge ever appeared" from "a challenge appeared and cleared."
Acceptance criteria: no fixed sleep-based waits in the fetch path (verified by grep — zero `page.wait_for_timeout()` calls) and bounded waits with clear timeout errors (190 tests passing, including 8 covering clear/never-clear/frame-detach scenarios with a strict FakeLocator, plus a test asserting `time.sleep` is never called on this path) are both met. The fast-network path is proven live across three validation runs (36130859283, 36132342460, 36138596780), all green with persist committing successfully each time. The slow/challenged path is proven only by unit tests so far — no live run has yet hit a real Cloudflare challenge to exercise `_wait_out_challenge`'s second stage in production.
Structural finding surfaced during this work, tracked separately as T-43 (#61): `data/scrape_health.jsonl` can't be read post-run because `actions/checkout`'s default `clean: true` wipes it before the downstream persist job's checkout finishes.
**Held open, not closed:** closing now would mean accepting unit-test-only evidence for the exact claim T-22 exists to prove. Issue #31 stays open until `challenge_wait_entered` is observed flipping true in a real scheduled run's log output, ideally during BF week when challenge pressure is highest.

## T-23 (#30): AI semantic schema and deterministic-verdict invariant — closed
`AIDealEvaluation` Pydantic model validates LLM output beyond key presence (enum membership, score ranges, boolean types, length limits) and `enforce_deterministic_invariant()` guarantees the rule engine's `rule_verdict` can never be overridden by the model. Commit f76c543. Issue #30 closed.

## T-24 (#32): golden adversarial AI dataset + per-call audit logging — closed
Added a versioned evaluation set covering malformed JSON, contradictory verdicts, prompt injection in a product title, Romanian diacritics, oversized text, wrong types, and hallucinated currency, run in CI. Every model call now logs model/version, prompt template version, deterministic inputs, latency, raw-output hash, parsed result, fallback path, and final policy decision to `data/ai_audit.jsonl`. Commit 9ac8bcb. Issue #32 closed.
Known gap inherited by this logging, not fixed here: `data/ai_audit.jsonl` is untracked and wiped by the same `actions/checkout` git-clean behavior as T-43 (#61) above — tracked there, not reopened against T-24.

## T-33 (#44): AI-provider dual-failure alerting — closed
Detects a same-run dual AI-provider failure (`ask_hf()` and `ask_ollama()` both failing on the same `get_analysis()` call) and queues a distinct operational alert — reusing T-09's `data/scrape_health_alerts.json` handoff file and notify.py's existing delivery path rather than a second Telegram integration, since analyze.py only has `HF_TOKEN` in monitor.yml. Appends to the file instead of overwriting it, since scrape.py writes it first in the same job. The alert write is best-effort and never blocks `get_analysis()`'s return value, covered by a dedicated test where the write itself fails and the deterministic result still comes back correctly.
Tests: 32/32 in test_analyze.py, 235/235 across the full suite, ruff clean. No live dispatch test run: this change only appends to a local file, and the Telegram send path (notify.py's `_send_health_alerts`/`_send_with_retry`) is pre-existing, already tested, and untouched. Commit: cc94e4a. Issue #44 closed.

## Sprints completed
Sprint 0 (foundations), Sprint 1 (correctness), Sprint 2 (notification reliability), Sprint 3 (security P0) — all closed, no action needed.

# bf-price-monitor — Current Handoff (2026-09-25)

## T-22/#31 diagnostic and T-44/#62 filed: challenge-detection selectors target a retired Cloudflare pattern
Following up on #31's remaining unproven acceptance criterion (whether `_wait_out_challenge` correctly clears a real Cloudflare challenge), ran a genuine, unmodified `fetch_with_browser` -> `is_challenge_page` -> `_wait_out_challenge` code path against `https://nowsecure.nl`, a public Cloudflare-challenge test page, via a new isolated diagnostic script (not wired into watchlist.json, main(), or any workflow). Three runs, all under 5 seconds, returned real page HTML with no challenge ever detected and the bounded wait never entered.
Root cause: `nowsecure.nl` now renders Cloudflare Turnstile as an in-page widget (`cf-turnstile` div, Cloudflare's own always-passing test sitekey), not the classic full-page interstitial (`#cf-challenge-running`, `iframe[src*='challenges.cloudflare.com']`) that `is_challenge_page()` and `_wait_out_challenge()` target. This produced no closing evidence either way for #31 — it's a detection-currency gap in the test target and, potentially, in production detection generally, not a bug in #31's bounded-wait mechanism itself, which is correctly implemented for the pattern it targets.
Filed T-44 (#62) to track modernizing `is_challenge_page()`/`_wait_out_challenge()` to also recognize in-page Turnstile/Managed Challenge patterns, linked as a sub-issue of #7 alongside every other Sprint 5 task. Cross-referenced on #31 via comment (no label/milestone/state change on either issue). Committed the diagnostic script as a reusable harness at `scripts/diagnostics/test_challenge_wait.py` (outside pytest's `testpaths = ["tests"]` scope). Commit: 250e819. #31 remains open and untouched otherwise, still held pending a real Cloudflare challenge being observed live.

## T-44 (#62): modern Cloudflare challenge detection — closed
Live captures (2026-09-25) showed a modern Managed Challenge attaches its Turnstile iframe inside a closed shadow root and has no `#cf-challenge-running`, so the old wait selector matched nothing: the real `_wait_out_challenge()` gave up after 1.5s on scrapingcourse.com and nopecha.com. Detection now keys on `_cf_chl_opt` and the `/challenge-platform/h/<x>/orchestrate/` script (generic Cloudflare markers ruled out: PC Garage and Flanco load Bot Management's passive `jsd/main.js` on every normal page), the wait uses attached/detached, and in-page Turnstile embeds are logged but no longer treated as blocking (the old markers discarded such pages). Four fixtures trimmed from live captures under `tests/fixtures/cloudflare/`; full suite and ruff clean. Commit: 589daf9 (`Refs #62`). Closed as completed with an evidence comment, all four acceptance criteria ticked, board status Done.
Live validation: both managed challenges now hold the full bounded wait; nowsecure.nl and demo.turnstile.workers.dev are returned with a non-blocking log line. Dispatch run 36163000064 on 589daf9: eMAG 59 parsed / 0 failures, PC Garage 20 / 0, Flanco 12 / 1 (same failure as the pre-fix baseline), `challenge_wait_entered=false` for all three — no false positives. Consequence for #31: only runs on 589daf9 or later count as valid evidence, since before it `challenge_wait_entered` could never become true on a modern challenge. As of 18:08 UTC no scheduled run had completed since the 15:54 UTC artifact fix (the 16:00 slot never fired; recent history shows GitHub firing roughly 4-5 of the 12 daily cron slots).

## Sprint 5 (Performance & AI, due 2026-09-30) — 7/8 complete
Parent #7 still open. Closed: T-21 (#29), T-23 (#30), T-24 (#32), T-33 (#44), T-42 (#60), T-43 (#61), T-44 (#62). Open:
- T-22 (#31) — implementation complete, held open pending a real Cloudflare challenge being observed live in a run on 589daf9 or later (see T-44 entry above).

## Blocked / gating
- T-31 (P0) Black Friday go/no-go review — blocked on Sprint 3-5 completion (Sprint 5 now 7/8; only T-22 remains open).

## Last session action items
- Watch for a live Cloudflare challenge in scheduled-run logs to close T-22 (#31); only runs on 589daf9 or later count.
- Scheduled runs fire far less often than the 2-hourly cron asks, which limits #31 evidence before the 2026-09-30 deadline.
- New issue #54 filed (unrelated tech debt): Quality Gate runs ruff check but not ruff format --check.