# Decisions Log

## 2026-09-22 — Issue #5 stray comment
Comment on issue #5 reads "Check and update the names of my table from a AI Tuhoty" —
unrelated/garbled text, does not match issue content. Treated as noise, not acted on.
Revisit only if it recurs or a related issue references it.

## 2026-09-22 — Quality Gate does not enforce ruff format --check (issue #54)
Discovered while pushing T-17 (#25): `scripts/analyze.py` fails `ruff format --check`
but has passed CI through several merges, including four same-day Dependabot PRs
(#50-#53). Confirms the Quality Gate workflow only runs `ruff check` (lint), not
`ruff format --check` (formatting) — drift can accumulate silently between the two.
Filed as its own issue rather than folded into #25, since it's unrelated pre-existing
tech debt, not a regression from T-17. See #54 for the two remediation options
(add a format-check CI step + one repo-wide format pass, vs. explicitly documenting
formatting as unenforced).

## 2026-09-22 — analyze.py formatting drift deliberately deferred
While closing #25, only the one `ruff format --check`-flagged line in `analyze.py`
(the long `print(...)` in `main()`) was hand-fixed, committed alone as 8a1b5fd. Three
other pre-existing formatting issues in the same file — a missing blank line after
the `RawAlertCandidate` class, quote-style on the Romanian JSON-prompt string in
`build_omnibus_prompt`, and the `get_analysis(...)` signature wrapping — were left
untouched on purpose, because a full `ruff format` pass would have rewritten all four
spots in one commit, mixing unrelated cosmetic changes into a security-task commit.
This is why `ruff format --check scripts/analyze.py` still fails after 8a1b5fd — that
failure is expected and tracked by #54, not a sign the fix was incomplete.

## 2026-09-22 — T-18 re-prioritized ahead of T-16
T-18 re-prioritized ahead of T-16 — today's outage was a PyPI-fetch failure,
not a runner-hardening gap; a cached/immutable environment (T-18) would have
prevented it outright by removing the PyPI dependency from routine scheduled
runs. T-16 remains necessary but doesn't address this specific failure mode.

Unresolved watch item: PC-A1208 has a VPN adapter (PIA) with route metric
32000 vs LAN's 25 — unusually high, possibly indicating past tunnel
instability. Not fixed, not proven causal to the DNS failures. Revisit if
DNS errors recur.

## 2026-09-23 — T-35 Runner Outage Contingency Runbook
Implemented T-35 Runner Outage Contingency Runbook and emergency execution
harness (`scripts/run_emergency_local.ps1`). Validated Python >=3.11 floor
per pyproject.toml and verified graceful degradation when HF_TOKEN is
absent.

## 2026-09-23 — T-16 Self-Hosted Runner Hardening
Implemented T-16 Self-Hosted Runner Hardening. Added
`scripts/runner_cleanup.ps1` with -DryRun for terminating zombie
Playwright processes and clearing %TEMP% debris older than 24 hours.
Added `docs/runbooks/RUNNER_SETUP.md` documenting DACL removal of
Authenticated Users Modify rights and least-privilege service account
guidance. Added post-job cleanup step to monitor.yml.

## 2026-09-23: T-20 migration confirmed stock_status mapping matches scrape.py's
should_alert() semantics exactly (out_of_stock blocks alerts; all other values,
including missing, are alert-eligible) — no behavior drift introduced.
Known inherited limitation from T-19: canonical_products uses title-based
identity, so near-duplicate titles across retailers can collapse into one
product row (291 products vs 292 offers in the migrated dataset). Not a T-20
bug; proper cross-retailer identity is T-37/T-38 scope.

## 2026-09-24 — T-41 (#58): notify.py dedup id, outbox write path, cooldown_hours
Found while scoping T-40's multi-retailer fan-out (#57), filed separately since
these are pre-existing bugs, not fan-out design gaps. Kept the existing
sha256(url:price:site) dedup_key string format unchanged and layered the fix
on top of it — event_id is now uuid5 of that key instead of the URL alone —
rather than switching to a rounded-price key, so records already in the real
outbox keep matching their cooldowns after deploy. The photo and text send
paths now share one outbox write: PENDING is written before either attempt,
not after a successful text send, since that was the actual cause of the
photo path never appearing in the outbox. `_write_outbox_record` now returns
the record it wrote, and replay keeps that record (with its real timestamp)
instead of rebuilding one without it — the missing-timestamp version is what
crashed the cooldown check on replay. `cooldown_hours` is restored to `int`
on the Watch model (legacy schema still allows `number`, so a legacy value
like 1.5 would fail migration — no real watchlist entry has ever used a
non-integer value, so this wasn't backfilled). data/watchlist.json itself was
not touched: the Watch default, notify.py's fallback, and the migration
default all already resolve to 24, matching every real entry's prior value.
New convention introduced: _derive_sku() extracts SKU from URL path segments
in scripts/migrate_history_to_sqlite.py — T-38's SKU extraction work should
either reuse or explicitly supersede this.

## 2026-09-24 — T-40 (#57) Phase 3: seller_policy "trusted" = is_marketplace is False
Of the three seller_policy options scoped on #57, implemented Option 1
(first-party-verified gate) and explicitly rejected Option 3 (per-Watch
seller allowlist): no retailer today exposes a real per-listing seller to
allowlist against — eMAG's listing page reports `seller: None` on every
card (robots.txt disallows /product/, the only page with real seller text),
and PC Garage/Flanco are unconditionally first-party with no marketplace
program, so their cards are hardcoded `seller`/`is_marketplace: False` in
scrape.py. A per-seller allowlist would have nothing real to match against
until a retailer with actual per-listing marketplace sellers exists.
`seller_policy`'s type (`Literal["any", "trusted"]`) is therefore unchanged;
"trusted" now means the gate at scrape.py's alert-append site (next to
`should_alert()`) skips the alert unless `is_marketplace is False`. This is
a deliberate, visible degradation for eMAG, not a bug: a "trusted" watch on
eMAG will never alert until eMAG's seller identity becomes resolvable some
other way. A new `policy_blocked_count` stat, surfaced per store next to
`matched_count` in scrape_health.jsonl, makes that silence visible instead
of indistinguishable from "no price drop happened."
T-40's remaining scope — fan-out across multiple retailers per watch
(Phase 1's Option B main()-loop rewrite) — is explicitly NOT part of this
change and was not implemented. #57 is being closed for the seller_policy
piece only; the fan-out rewrite is tracked as a fresh issue if still wanted.

## 2026-09-23: data/price_history.db is git-ignored by design. It is
runner-local, regenerable state (via migrate_history_to_sqlite.py from
data/price_history.json), not a git-tracked artifact — matches T-18's
precedent of treating the venv cache as persistent-but-untracked runner
state. data/price_history.json remains the git-tracked source of truth
and disaster-recovery backup during the SQLite transition period.
Until T-37 fully cuts scrape.py over to SQLite-only writes, both files
will exist in parallel; JSON continues to be updated by the existing
pipeline and committed by monitor.yml's persist job as before.

## 2026-09-23 — Sprint 4 parent issue #6 shows closed with open sub-issues
Sprint 4 parent issue #6 shows as closed despite #48/#49/#55 remaining open
sub-issues — noting the discrepancy for whoever reviews Sprint 4 completion,
not fixing it here.

## 2026-09-23: Watch.drop_rule, Watch.drop_threshold made optional (default
None), Watch.track_all_time_low defaults to True. Confirmed via grep that
should_alert() in scrape.py never reads drop_rule/drop_threshold — they
have zero runtime consumers in the live alerting path. Needed to let
migrate_watchlist_to_modern.py construct valid Watch objects from the 4
real legacy entries, none of which populate these fields.

## 2026-09-24: watchlist_schema.json's modernWatchItem was missing
'enabled' in its properties despite Watch.enabled being a pre-existing
model field (not introduced by this task). additionalProperties:false
caused any dumped Watch with its default enabled value to fail schema
validation. Added enabled: boolean to modernWatchItem; left optional
since the model already defaults it to True.

## 2026-09-24: Promoting migrate_watchlist_to_modern.py's output to
data/watchlist.json was caught and reverted before commit. Root cause:
Watch/modernWatchItem have no 'site' field, so migrated entries lose
retailer identity entirely. scrape.py's main() reads item['site']
unconditionally (line ~1012) to select the retailer adapter per watch
and to disambiguate entries with identical queries but different
retailers (e.g. the two 'laptop asus vivobook' entries, tracked on
pcgarage and flanco respectively). Promoting the modern-format file as
currently defined would crash the next scrape run. Adding site support
to Watch/modernWatchItem/scrape.py is deferred as its own follow-up task
— not done as part of this migration to avoid scope creep under
time pressure. The migration script and modern schema are NOT safe to
promote to the live watchlist until that follow-up lands.

## 2026-09-24 — Session retrospective: recurring risks for future sessions
Three patterns worth carrying forward from T-37/#48 Phase 2, to avoid
repeating the same near-misses on future migration/schema tasks:
1. The ruff auto-fix hook silently strips import-only edits added before
   their usage lands in the same session — verify with a real test run
   (NameError, not just a clean diff) rather than trusting the diff alone.
2. Schema-valid is not the same as production-safe. Validate output
   programmatically against validate_watchlist() (or equivalent) before
   approving a migration for promotion — printed/eyeballed JSON caught
   neither the Decimal-string encoding bug nor the missing `enabled`
   property; only an actual validator run did.
3. Schema/model validation alone won't catch a field a downstream
   consumer (e.g. scrape.py's item["site"] read) depends on but the
   schema never declared. Grep actual runtime usage of a field before
   assuming a migration is "1:1, no behavior loss" — the site/retailer
   gap (#56) would have crashed production despite passing schema
   validation cleanly.

## 2026-09-24: #56 resolved via Option A (site field added to
Watch/modernWatchItem, scrape.py's existing one-site-per-watch loop
unchanged). Option B (fan-out across SCRAPERS registry per query,
filtered by seller_policy) was investigated and found feasible at the
adapter-registry level, but requires: (a) a new seller_policy semantics
decision — no "trusted retailer" concept exists anywhere in the codebase
today, (b) rewriting main()'s per-watch single-site tagging into
per-offer tagging across history/SQLite/alerts, (c) an unchecked
notify.py/analyze.py assumption of one-alert-per-watch-per-run that
needs verification before Option B is safe. Deferred to a new issue,
not attempted under Sprint 4's deadline. seller_policy and
Offer.retailer remain declared-but-unwired in the live path until that
future work lands.

## 2026-09-25 — T-37b (#55): delivery_attempts is additive audit storage only
Wired `DeliveryAttempt` into a new SQLite `delivery_attempts` table
(`bf_price_monitor/storage/sqlite.py`), written from `scripts/notify.py`.
Scope was kept deliberately narrow:

- `data/alert_outbox.jsonl` remains the sole operational authority for
  PENDING/in-flight state, crash replay, cooldown lookups, and dedup
  suppression. `delivery_attempts` never drives any of that logic — it is
  written *after* the outbox record that already governs behavior, purely
  for querying delivery history later.
- Only terminal outcomes are ever persisted (`delivered`/`failed`). No
  `pending` state was added to `DeliveryAttempt.final_state`, and no
  `deduped` rows are written either — cooldown/in-run-duplicate skips still
  write nothing anywhere, matching existing behavior; wiring "deduped"
  would mean adding new write calls at those skip sites, which is a
  behavior change out of scope for this issue.
- No `alert_decisions` table or FK was introduced. `AlertDecision` is still
  never persisted (dumped into the outbox's `alert_payload` only); the new
  table's `alert_decision_id` column is a plain indexed TEXT reference, not
  a SQL foreign key, since a strict FK against a nonexistent table would
  fail every insert (confirmed via `tests/unit/test_sqlite_storage.py`'s
  existing `sqlite3.IntegrityError` FK-enforcement test).
- `destination_ref` stores `sha256("telegram:" + chat_id)`, never the raw
  Telegram chat id, keeping the audit table safe to inspect without
  exposing a credential-adjacent value.
- `notify.py` opens its own `init_db(DB_FILE)` connection at `main()`
  startup and closes it in a `finally` block, since it runs as a separate
  CI step/process from `scrape.py` and has never had any SQLite access
  before this change — `scrape.py`'s open connection can't be reused
  across process boundaries.
- Deal alerts pass their existing `dedup_key` (sha256 of url:price:site)
  through unchanged; health alerts pass `dedup_key=None` rather than a
  manufactured key, since they have no "same offer" identity to key off of
  (this is also why `DeliveryAttempt.dedup_key` became optional).
- Health alerts' outbox `event_id` is a raw sha256 hex string (see
  `_health_event_id`), not a UUID, while `DeliveryAttempt.alert_decision_id`
  is typed `UUID`. `notify._decision_id_from_event()` parses deal event ids
  directly (already UUID-shaped) and falls back to `uuid5`-wrapping the
  health event id into a UUID otherwise — a small mechanical adaptation
  needed to satisfy the model's existing typing, not a scope change.
- `response_class` is only ever written as `"2xx"` (success) or `"unknown"`
  (failure) — `_send_with_retry`'s internal HTTP-status classification
  (429/5xx/network/permanent-4xx) is not surfaced to the outbox-finalization
  call sites that write `delivery_attempts`, and per-HTTP-retry
  instrumentation is deliberately deferred rather than added here.
- `DeliveryAttempt.attempt_number` is never hard-coded and never read from
  the outbox's own `attempt_count` field. That JSONL field only ever
  describes attempts *within* one PENDING/terminal pair (reset to 0/1 every
  cycle, per #58) and can't distinguish a genuinely new delivery cycle for
  the same event — a deal re-alerting once its cooldown expires, or a
  failed health alert retried in a later run — from the first one. A new
  cycle's number is instead allocated atomically at the storage-write
  boundary: `storage.sqlite.record_delivery_attempt_new_cycle()` computes
  `COALESCE(MAX(attempt_number), 0) + 1` for that `alert_decision_id`
  inside the same `INSERT ... SELECT` statement that writes the row, so
  allocation and insertion can never interleave with a concurrent writer's
  allocation for the same id — unlike a separate `SELECT MAX` followed by
  its own `INSERT`, which leaves a window where two callers could read the
  same max and collide on the `UNIQUE(alert_decision_id, attempt_number)`
  constraint. `notify.py` calls this for every live write and never queries
  `MAX(attempt_number)` itself. A second, explicit path,
  `record_delivery_attempt()`, is for replaying an *already-known*
  `attempt_number` — an idempotent rewrite of a specific already-audited
  row — and upserts that exact `(alert_decision_id, attempt_number)` pair
  rather than allocating a new one.

This keeps the migration pattern consistent with `price_history.json` +
SQLite `price_history.db` dual-write (T-37): the new store is additive and
observational, not a replacement, until a future issue decides otherwise.

## 2026-09-25 — T-21 (#29): one browser per run, one context per retailer

Replaced `fetch_with_browser`'s previous per-fetch full browser launch with a
run-scoped `_BrowserState` that reuses one stealth-wrapped Chromium browser
and per-retailer `BrowserContext`s across the whole `scripts/scrape.py`
`main()` invocation, closing the gap between the two GitHub Actions steps
that previously left orphaned Chromium processes for `scripts/runner_cleanup.ps1`
to mop up (T-16, #26) and made every fetch pay a fresh launch cost.

- **One browser per run, lazily started.** `_BrowserState.start()` launches
  Chromium on the first real `get_context()` call, not unconditionally in
  `main()`. A watchlist run with no pcgarage/flanco items never touches
  Playwright at all — this also sidesteps a real conflict: eagerly starting
  Playwright's sync API inside `main()` broke every test that fully mocks
  `SCRAPERS` and runs under pytest's `anyio` plugin, since Playwright's sync
  driver refuses to start inside a process with an active asyncio event loop.
- **One `BrowserContext` per retailer, reused across normal fetches and
  403/429 retries.** `fetch_with_browser`'s inner status-code retry loop
  keeps calling `get_context(site_name)` on the same cached context, so any
  challenge/session cookies a site sets while working through a 403/429 are
  still present on the next attempt. Different retailers never share a
  context — cookies, storage, and any challenge state are isolated per site.
- **Context reset only on a scraper-level exception retry, not on inner
  403/429 retries.** `with_retry` gained an optional `site_name` parameter;
  when set, a caught exception calls `_browser_state.reset_context(site_name)`
  before the outer retry re-invokes the scraper function. This discards and
  closes only that site's context (a fresh one is created lazily on the next
  `get_context` call) — the reasoning being that an *unhandled exception*
  signals the context itself may be in a bad state (e.g. stuck mid-navigation
  or wedged in a challenge loop), so the retry deserves a clean context,
  whereas an in-band 403/429 status code is an expected, recoverable
  condition where discarding cookies would be counterproductive. Other
  retailers' contexts and the run's browser are untouched by a reset.
- **A fresh `Page` per fetch attempt, always closed in `finally`.** Only the
  context (cookies/storage) is long-lived; each navigation gets its own page,
  closed whether `goto()`, `content()`, or challenge-polling succeeds,
  returns early, or raises.
- **Run-level cleanup is unconditional.** `main()` now wraps `_run(watchlist)`
  in `try`/`finally`, calling `_browser_state.close()` regardless of how
  `_run` exits. `close()` tears down every remaining context, the browser,
  then the stealth/`sync_playwright` context, in that order, so a mid-run
  exception (a bad scrape, a file-write failure) can never leak a live
  browser process for the next scheduled run to inherit.
- **`fetch_with_browser`'s public contract is unchanged** — still
  `(url: str, site_name: str) -> str | None` — so no adapter or call site
  needed to change; only its internal resource acquisition changed from
  "launch a browser" to "borrow a run-scoped context and open a page."
- **Live before/after performance measurement (actual launch-time savings
  across a real multi-retailer run) is deferred.** This change was verified
  via unit tests against fakes (`tests/test_browser_state.py`) and the full
  existing suite, not via a live `workflow_dispatch` run — that requires
  separate explicit approval, per the standing rule that no live retailer
  scrape runs without it.
- **The first live validation run (2026-09-25) exposed an invalid
  `context.new_page(user_agent=...)` call**, which broke PC Garage and
  Flanco entirely (Playwright's real `BrowserContext.new_page()` takes no
  arguments). `user_agent` remains configured only at
  `browser.new_context(...)` in `_BrowserState.get_context`, unchanged from
  the original design. `tests/test_browser_state.py`'s `FakeContext` was
  tightened from a permissive `new_page(self, **kwargs)` to the real
  zero-argument `new_page(self)` shape so this class of bug fails in unit
  tests instead of only surfacing live. #29 remains In Review pending a
  successful revalidation run against the corrected code.