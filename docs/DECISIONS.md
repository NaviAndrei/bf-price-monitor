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

## 2026-09-28 — #54 resolved: Quality Gate enforces ruff format --check (option a)
Supersedes the two #54 entries above. By 2026-09-28 the repo-wide baseline was
already clean (`ruff format --check .` passes on every file with the locked ruff
0.16.8; the analyze.py drift was fixed by later commits), so option (a) needed no
formatting-only commit — only the enforcement step. `quality.yml` now runs
`uv run ruff format --check .` right after `ruff check`, and README "Quality gate"
lists the exact local commands. Rejected option (b) (documenting formatting as
unenforced) because drift already slipped through four Dependabot merges once.

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

## 2026-09-28 — T-40 follow-up (#59): multi-retailer fan-out per watch
Fan-out is opt-in through a sentinel value, `site: "all"` (`FANOUT_SITE` in
scrape.py), which runs the watch's query through every retailer in the
`SCRAPERS` registry. Every other `site` value keeps the one-scraper path
exactly as before. Chosen over adding a new `sites`/`retailers` list field
because `Watch.site` has to stay a required string (explicitly out of scope
on #59), and a sentinel needs no schema, model or migration change: the
current validator already accepts any non-empty string. main() now iterates
(watch, retailer) pairs, so per-store health stats, the quarantine skip, and
every per-offer tag (history `site`, SQLite `retailer`, alert `site`) key on
the retailer that produced the offer. One quarantined retailer skips only
itself, not the whole fan-out watch.
The "one alert per watch per run" assumption flagged on #57 was checked
against the live code. Nothing depends on it: scrape.py already emitted one
alert per matching listing, analyze.py judges each alert on its own, and
notify.py's dedup key and event id are per offer. #58's in-run dedup covers
the same offer arriving twice. What did break is that three notify.py lookups
were keyed on the watch's `site` but read `alert["site"]`: channel routing,
cooldown, and the derived `AlertDecision.watch_id`. Under fan-out those would
have silently fallen back to Telegram-only routing and the 24h default
cooldown, and split one watch into several watch ids. Alerts now carry a
`watch_site` field (the watch's own configured site), which notify.py uses
for those three lookups, falling back to `site` when it is absent. For a
single-site watch `watch_site == site`, so every existing key (dedup_key,
event_id, watch_id, cooldown, channels) is byte-identical to before.
seller_policy under fan-out: the watch's policy applies uniformly to every
retailer it fans out to, and the unchanged gate is evaluated per offer
against that offer's own `is_marketplace`. A "trusted" fan-out watch
therefore alerts on PC Garage and Flanco but never on eMAG, whose
`is_marketplace` is always None, and each blocked eMAG offer is counted in
eMAG's own `policy_blocked_count`. Rejected: skipping retailers that can
never satisfy "trusted". That would hide the gap instead of counting it, and
would stop recording eMAG observations, which history and the 30-day
reference price still need.
Addendum (same day): placeholder scrapers are excluded from fan-out. Altex's
scraper is a stub that always returns zero products, and the Critical
Selector Drift check quarantines any store with two zero runs while another
store succeeds, so fanning out to it would have raised a false "SCRAPER
BREAKDOWN: Altex" alert after two runs. The stub is marked in the `SCRAPERS`
registry itself with `placeholder_scraper(...)`, and fan-out skips any
marked entry, so a future stub only needs the marker, never a name list in
main(). A placeholder that is never scraped gets no health record, so it
can never be quarantined through fan-out. Altex stays in the registry, and a
watch explicitly set to `site: "altex"` behaves as before. report.py treats
an `"all"` watch as matching any retailer's products by query, reusing
`FANOUT_SITE` rather than a second sentinel.

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

## 2026-09-25 — T-26 (#35): Teams, email and ntfy channels share the outbox
Added Microsoft Teams (Workflows webhook), email (SMTP) and ntfy delivery
next to Telegram, routed per watch via a new `Watch.channels` field
(`modernWatchItem.channels` in the schema; default `["telegram"]`).

- **No Apprise.** Apprise would have hidden each service's status codes
  behind a single boolean, which makes the retry/dead-letter
  classification impossible to keep identical across channels (acceptance
  criterion 3). Teams and ntfy go through the existing `requests` path;
  email uses stdlib `smtplib`. No new dependency.
- **One retry loop for every channel.** `_deliver_with_retry` owns the
  attempt budget, backoff and DLQ write. Each transport only classifies one
  attempt as ok, transient or permanent. HTTP: any 2xx is success (Teams
  Workflows answers 202, ntfy and Telegram 200); 429 (with Retry-After), 5xx
  and network errors are transient; other 4xx are permanent. SMTP: a 4xx
  reply is transient, a 5xx reply (including auth failure 535) is
  permanent, and connection-level errors are transient.
- **One outbox event per (decision, channel).** Telegram keeps the bare
  pre-T-26 event id so existing SENT history and cooldown windows still
  match after the upgrade; other channels use `<decision id>:<channel>`.
  Each record carries its `channel`, which replay uses to pick the
  provider. Cooldown is checked per channel, so a failed Teams delivery is
  retried next run even though Telegram's copy is inside its cooldown.
- **A PENDING record whose channel is no longer configured is
  dead-lettered on replay** (`final_status_code: "channel_not_configured"`)
  rather than left PENDING to be retried every run.
- **`delivery_attempts` stays one table.** Every channel of one decision
  shares the same `alert_decision_id` (the `:<channel>` suffix is stripped),
  and `channel` tells the rows apart. Consequence: `attempt_number` counts
  delivery cycles across all of that decision's channels, not per channel.
  `destination_ref` hashes the webhook URL, ntfy topic URL or recipient
  list the same way it already hashed the chat id.
- **Secrets stay out of storage and logs.** The ntfy topic is added at send
  time and never written to the outbox's `send_payload`; a Teams webhook's
  `sig=` value is redacted from DLQ and log reasons. SMTP failure reasons
  have the configured login, sender and every individual recipient
  redacted (case-insensitively), because GitHub masks only a secret's exact
  value and a server reply often echoes one address out of a
  comma-separated `EMAIL_TO`. Secret-bearing dataclass fields (SMTP
  credentials and addresses, a channel's raw destination) are `repr=False`.
- **Card version 1.2.** The card uses only 1.0-era elements (TextBlock,
  FactSet, Action.OpenUrl), so it declares 1.2, matching Microsoft's
  Workflows-webhook sample; 1.5 support in the post-card action was not
  verified.
- **Routing merges watches.** Because T-41's in-run skip keeps only the
  first alert per offer, an offer matched by several watches goes to the
  union of their channels. Health alerts remain Telegram-only.
- **Known limits.** Teams' 202 is asynchronous: the flow can still fail
  after accepting the request, and that failure is invisible to the outbox.
  A 202 proves acceptance by the workflow, not posting or rendering in the
  channel; live Teams posting and rendering have not been verified.
  Whether ntfy sends a Retry-After header on 429 was not verified; without
  it the normal backoff applies. The legacy flat-array schema did not gain
  `channels`, since production already uses the modern format.

## 2026-09-26 — Remove Microsoft Teams channel (#35 scope change)
Removed Teams entirely as a follow-up to T-26 (above): `build_teams_card`,
the `teams` entry in `DEAL_PAYLOAD_BUILDERS`, the `teams` provider in
`_load_providers`, `TEAMS_WEBHOOK_URL` from the workflow's env and README,
and `"teams"` from both the `Watch.channels` `Literal` and the watchlist
JSON Schema enum. Telegram, email and ntfy, and their shared outbox,
retry-with-backoff and dead-letter handling from T-26, are unchanged.

- **Why remove rather than fix.** This is a scope decision, not a bug fix:
  Teams support is no longer wanted, not broken. T-26's "known limits" note
  above (a 202 only proves the workflow accepted the request, not that it
  posted or rendered) still stands as the reason it was never verified live
  — but that was not the reason for removal.
- **Old PENDING/DEAD_LETTER outbox records with `channel: "teams"` are
  handled safely, not specially.** Replay's existing
  `channel_not_configured` path (T-26, since `_load_providers` no longer
  registers *any* provider whose channel isn't backed by a configured
  secret) already dead-letters a PENDING record whose channel has no
  provider, without ever attempting delivery or touching a webhook URL.
  Removing the `teams` provider makes every such record take that path
  automatically; `test_replay_of_unconfigured_channel_dead_letters_instead_of_hanging`
  locks this in for a channel value that no longer exists anywhere else in
  the codebase.
- **The Teams `sig=` redaction pattern in `_SECRET_PATTERNS` was deleted**,
  not just left unused — it matched a Teams-specific webhook URL shape and
  had no other purpose.
- **`data/watchlist.json` needed no migration.** No live watch referenced
  `"teams"` in its `channels` list before this change.

## 2026-09-28 — Runner-local runtime-state persistence (#42 B2 blocker)
The Black Friday readiness rehearsal (#42) went No-Go on two blockers; this
closes B2. `actions/checkout` cleans the self-hosted runner's workspace on
every run, which erases `data/price_history.db`, `data/scrape_health.jsonl`
and `data/alert_outbox.jsonl` before the next run starts — the exact files
the cooldown, dead-man and scraper-quarantine safeguards read from the
previous run. Every rehearsal run was therefore starting from empty state.

- **GitHub artifacts were rejected as the persistence mechanism.** This
  repository is public. Artifact *listing* is visible to anonymous callers,
  and GitHub's own documentation states artifact *download* requires only
  read access to the repository — which every signed-in GitHub account
  already has on a public repo. An artifact cannot hold operational state
  that safeguards depend on without also making that state readable by
  anyone with a GitHub account, so it fails the privacy requirement outright
  and was never implemented.
- **`clean: false` on `actions/checkout` was rejected.** It would keep the
  workspace itself dirty between runs, which persists *everything*
  untracked in the workspace, not just the three files the safeguards
  need — including any accidental leftovers from a previous run's crash —
  and offers no ACL boundary, no validation, and no atomic promotion. It
  also could not be scoped to the required files, unlike a dedicated state
  directory outside the workspace entirely.
- **The state root is `C:\bf-monitor-runtime-state`, outside the job
  workspace, so `actions/checkout`'s clean never touches it.** Access is
  restricted to exactly `NT AUTHORITY\SYSTEM` and `BUILTIN\Administrators`
  (Full Control) plus this runner's own service SID,
  `NT SERVICE\actions.runner.NaviAndrei-bf-price-monitor.PC-A1208` (Modify),
  granted via `sc.exe sidtype unrestricted` rather than the shared
  `NETWORK SERVICE` identity every other NETWORK SERVICE-logon service also
  uses. `bf_price_monitor/runtime_state.py` reads and validates this ACL
  itself before every restore or save — an unprotected ACL, an inherited
  entry, an unexpected owner, or any principal outside that set (in
  particular `NETWORK SERVICE`, `BUILTIN\Users`, `Authenticated Users` or
  `Everyone`) disables state persistence for that run (`state_status =
  "disabled_insecure_state_dir"`) rather than operating against a directory
  it can't prove is private.
- **Durable state is exactly three files, never more.** `price_history.db`
  is captured via the SQLite backup API against a read-only connection to
  the live WAL-mode database, never a raw file copy, so a snapshot is
  always internally consistent even if the source is mid-write.
  `scrape_health.jsonl` and `alert_outbox.jsonl` are copied and validated
  line by line. `alerts.json`, `formatted_alerts.json`,
  `scrape_health_alerts.json`, `dlq.jsonl`, `extraction_failures.jsonl` and
  `ai_audit.jsonl` are deliberately excluded — they are per-run report
  output, not state a safeguard reads back on the next run.
- **Restore runs before scraping; save runs with `if: always()` after
  notify**, so a run that crashes partway through still gets its outbox and
  health state captured, instead of losing a PENDING or SENT record that
  the next run's cooldown or replay logic needed to see. A save is only
  *promoted* to `CURRENT` after its candidate passes every check: SQLite
  `integrity_check`, JSONL line-by-line parsing, and a manifest of
  per-file SHA-256 hashes matching the files actually on disk. `CURRENT` is
  switched via `os.replace`, so no reader ever observes a half-written
  pointer. A candidate that fails validation is moved to a `quarantine/`
  subdirectory instead of being deleted, and the previous valid `CURRENT`
  is left completely untouched.
- **Retention is the last three valid snapshots; quarantine is not
  pruned.** Quarantined snapshots exist for forensic inspection after an
  incident, not as a resource to reclaim automatically. This is a known
  gap, not an oversight: over months of runs a busy quarantine directory
  will accumulate and eventually needs a manual cleanup pass.
- **Known limitation: rebuilding the runner or renaming its service breaks
  this without warning.** The ACL is pinned to the exact service SID
  string `NT SERVICE\actions.runner.NaviAndrei-bf-price-monitor.PC-A1208`.
  If the runner is ever re-registered under a different name, or the
  service is rebuilt from scratch, the new service gets a new SID and the
  existing ACL grant no longer matches it — `runtime_state.py` will see an
  ACL with no valid `modify_principal` entry and disable persistence
  (`disabled_insecure_state_dir`) rather than silently granting the new
  service access. Recovering from this requires manually re-running the
  Phase 2 SID/ACL hardening steps against the new service, then verifying
  the ACL again before the next run.
- **Verified locally only.** All 30 tests in `tests/test_runtime_state.py`
  pass, including a two-run simulation that wipes the workspace between
  runs and confirms cooldown, dead-man detection, scraper quarantine and
  SQLite observation continuity all survive using the real
  `scrape._quarantined_stores` and `notify._find_cooldown_block` functions.
  None of this has yet run against the real `C:\bf-monitor-runtime-state`
  directory or the real runner service SID — that requires an actual
  monitor run, which was explicitly out of scope for this change.

## 2026-09-28 — T-28 (#37): feedback labels via scheduled getUpdates polling
The monitor runs only as scheduled GitHub Actions jobs with no host listening
between runs, so button presses are pulled with Telegram `getUpdates` in a new
`Collect alert feedback` step rather than received by a webhook, which would
need a public HTTPS endpoint (paid or always-on host). Trade-offs accepted
and documented in docs/feedback-labels.md: a delayed spinner until the next
run, and presses lost when runs are more than 24 hours apart (Telegram's
retention). An always-on `collect --loop` worker covers Docker mode (#38).
Key choices:
- Labels live in the existing runner-local `price_history.db`, not a new
  file, so the #42 runtime-state snapshot carries them. Save now also
  compares per-table row counts before promoting a snapshot.
- First versioned migration mechanism (`PRAGMA user_version`, one
  transaction per version). The base `_SCHEMA_SQL` stays as implicit
  version 0.
- Referential check against `delivery_attempts` (a delivered Telegram row
  for the decision id), since there is still no `alert_decisions` table
  (see T-37b).
- Identities are stored as HMAC-SHA256 with a per-database random salt, not
  plain sha256 like `delivery_attempts.destination_ref`. A plain hash of a
  ~10-digit Telegram id is brute-forceable. `destination_ref` was left
  unchanged (out of scope).
- The stored offset is trusted for only 6 days, because the Bot API docs say
  `update_id` restarts at a random value after a week with no updates.
  Idempotency therefore keys on `callback_query.id`, never on `update_id`.

## 2026-09-28 — T-38 (#38): Docker mode on python-slim + headless shell, not the Playwright image
The issue blueprint's base, `mcr.microsoft.com/playwright/python`, bundles
Chromium, Firefox and WebKit, which contradicts the "Chromium only"
acceptance criterion. Instead the image is `python:3.12-slim-bookworm`
(digest-pinned) plus `playwright install --with-deps --only-shell chromium`.
Per the Playwright docs, the headless shell is what `launch(headless=True)`
uses. Dependencies come from `uv sync --frozen --no-editable` in a builder
stage, so the image installs exactly `uv.lock`, and CI verifies that per
distribution.
- The blueprint's `bf_price_monitor.cli.daemon` does not exist. The
  entrypoint is instead `scripts/run_loop.py`, which runs the same three
  scripts as `monitor.yml` as child processes, so there is still one
  pipeline.
- Chromium runs without its sandbox (the Playwright default). Container
  hardening (UID 10001, `cap_drop: ALL`, `no-new-privileges`, read-only
  root) replaces a custom seccomp profile. `--ipc=host` was rejected in
  favor of `shm_size`.
- Nothing is published; the image is built and smoke-tested on
  ubuntu-latest (`docker.yml`). `monitor.yml` is untouched, and a test
  asserts it never references Docker.
- Docker Desktop was not running on the development machine, so the
  GitHub-hosted workflow is the clean-machine acceptance test of record.

## 2026-09-28 — T-38b (#49): product identity adapters stay SKU-compatible
Per-retailer SKU adapters (`bf_price_monitor/identity.py`) replace the ad-hoc
last-segment derivation in `scrape.py`. This explicitly supersedes the
`_derive_sku` convention flagged by T-41, while returning byte-identical SKUs
for all 307 URLs in history, so SQLite offer ids do not churn. A test guards
this.
- Evidence contradicted the issue text. Real PC Garage URLs have no
  `[slug]-[ID]` numeric id, so the slug is the identity.
- The fingerprint is `pid-v1:` + sha256(`retailer:sku`), the issue's
  formula plus a version prefix.
- Fuzzy title matching is a scored fallback for cross-retailer links only.
  Running it over real data exposed chassis-code false positives (same
  E1504FA, different CPU), which led to the CPU, capacity and
  unverified-spec penalties. The result is 6 accepted pairs, all consistent.
- Not done, and deliberately out of scope: re-keying SQLite
  `canonical_products` away from titles, or persisting fuzzy links. Both
  change stored product ids and need their own migration decision. See
  docs/product-identity.md.

## 2026-09-28 — T-29 (#36): anomaly pilot is an offline, label-gated extra
scikit-learn and ruptures are an optional `anomaly` extra locked in
`uv.lock`, installed only by the dedicated `anomaly-pilot` CI job. The
monitor (`uv sync --frozen`) and the Docker image never install them. This
keeps the numeric stack off the self-hosted runner and out of the
production dependency surface. Tests assert that production paths never
reference the pilot.
- The blueprint's numbers (contamination 0.03, score −0.65, PELT
  `l2`/`min_size=3`) are recorded as configurable hypotheses, not
  constants. Each report carries config, data and environment
  fingerprints.
- The split is chronological, with causal features. Preprocessing and the
  model are fit on train only, the threshold is chosen on validation only,
  and the test split stays sealed until `--unseal-test`.
- The deterministic baseline is the recorded `delivery_attempts` decisions,
  not a re-implementation of `rule_verdict`.
- Labels only exist for sent alerts, so metrics are explicitly conditional
  on that selection. A 30-label, two-class gate blocks precision/recall
  rather than estimating them. No synthetic labels outside unit tests.
- #36 stays open: its written precision/recall evaluation needs real #37
  labels. See docs/anomaly-pilot.md.

## 2026-09-28 — T-36 (#47): retro metrics are generated, never estimated
`scripts/generate_retro_metrics.py` computes the post-Black-Friday numbers
from the files the monitor already keeps: `scrape_health.jsonl`,
`alert_outbox.jsonl`, the SQLite store (opened `mode=ro`) and an optional
`gh run list` export. It adds no new telemetry and no production code path.
- Missing or empty inputs are reported as `no_data` with a reason, never
  as 0. Uptime needs health history from before the window end, because a
  missing file and a total outage look the same.
- Uptime is the share of elapsed 2-hour cron slots with a recorded run.
  This needs no GitHub API. Workflow success rate comes from the optional
  runs export and is reported separately.
- The feedback acceptance rate uses the #36 relevance policy, so the two
  can't disagree on what "relevant" means.
- "Money saved" is an explicitly caveated estimate: self-reported
  `purchased` labels, valued against the prior 30-day median observed
  price. No receipt data exists.
- Outputs go to git-ignored `data/exports/` and contain counts only. They
  hold no URLs, titles, rater references or chat ids.
- #47 stays open. docs/retros/2026-black-friday.md is a template until the
  event's real data fills it and the carry-over items are filed.

## 2026-09-28 — T-47 (#66): cross-retailer links persist as an additive
table, not a `canonical_products` re-key
`resolve_offer()` (#49) now runs on every SQLite write
(`_resolve_product_id` in `bf_price_monitor/storage/sqlite.py`), but the
issue's own literal task ("re-key `canonical_products` from title to
fingerprint") is deliberately not what got built.
- The calibration run recorded in #49's decision only ever produced
  `medium`-confidence fuzzy matches (0.75–0.85), never `high`. Automatically
  rewriting `canonical_products`/`offers` rows on a medium-confidence guess
  risks silently mixing two different products' price history — exactly
  the "schema-valid is not production-safe" trap this file already warns
  about elsewhere.
- Split by confidence instead: `sku_exact` matches (the same retailer+sku
  seen before) and `fuzzy_title` matches at or above `HIGH_CONFIDENCE`
  (0.85) redirect that one offer's `product_id` to the matched product, and
  the redirect is logged to a new `product_id_remaps` table (old id, new
  id, offer, reason) so it can be reversed with a plain `UPDATE`. Anything
  below `HIGH_CONFIDENCE` is recorded in a new `cross_retailer_links` table
  (fingerprint → linked product, confidence, method, reasons) but never
  touches `canonical_products` or `offers` — the link is durable and
  queryable without ever being a destructive merge.
- Both tables are additive (migration version 2); no existing row's `id`
  changes shape, so offer ids from #49's compatibility guarantee still hold.
- A useful side effect: `sku_exact` also fixes a pre-existing bug where the
  same offer's `product_id` recomputed from `uuid5(title)` on every write,
  so a retailer editing its listing's title text silently created a new
  canonical product. It's now pinned to the offer's first-seen product id.
- Tests: `tests/unit/test_sqlite_storage.py` covers all three paths (high
  confidence merges into one row, medium confidence links without merging,
  sku_exact survives a title edit) plus durability across a reopened
  connection.

## 2026-09-28 — T-32 (#43): BF-week cadence is a variable-gated second cron, not a hardcoded date

The 30-minute Black Friday cadence is a second `*/30 * * * *` cron in
`monitor.yml`. A small `gate` job (`scripts/cadence_gate.py`) lets it scrape
only while "now" falls inside the `BF_PEAK_START_UTC`/`BF_PEAK_END_UTC`
repository variables. The 2-hour cron and manual dispatch always pass.

- **No hardcoded BF date.** The three retailers set their own campaign
  dates and haven't announced 2026's. A wrong baked-in date is worse than
  an unset variable, so the gate fails closed: unset, unparsable, or naive
  (no UTC offset) boundaries mean the 30-minute cron does nothing.
- **Rollback is a variable edit, not a deploy.** Deleting
  `BF_PEAK_START_UTC` drops back to the 2-hour cadence on the next trigger.
  Full escalation ladder: `docs/runbooks/BF_WEEK_CADENCE.md` section 4.
- **Rejected: editing the cron line for BF week.** It needs two commits at
  the right moments and a commit to roll back. Also rejected: a
  per-run "sleep and loop" inside one job, which would hold the
  self-hosted runner for hours.
- **Request safety** (`scripts/scrape.py`): watch jitter is now 4–9s; there is
  a 5s per-store floor between requests (`_pace_domain`); the user agent
  rotates across Chrome 131/130 on Windows; and 403/429 counts are recorded
  per store in `scrape_health.jsonl` (`fetch_attempts`, `rate_limit_hits`).
  A store above a 5% rate is skipped for 30 minutes from that run's start.
  Like the T-09 quarantine, it's derived from history each run, not stored
  as a flag. The pacing timestamps live on `_RunState`, so the existing
  per-test `reset()` isolates them.
- **Known gaps, deliberately not fixed here:** the cooldown counts from the
  flagged run's start, so at the 30-minute cadence it mostly catches the
  duplicate even-hour run and manual dispatches rather than skipping a
  whole cycle. There's no per-watch priority, so all watches get 30 minutes
  during the window. Trial evidence covers two local back-to-back runs
  (zero 403/429, zero challenges on all three stores), not a sustained run
  from the production runner.

## 2026-09-29 — #69: GitHub Actions is the sole production feedback collector; callbacks are audited append-only

- **Decision.** Only the GitHub Actions workflow runs `feedback.py collect`
  against the production bot token. The Docker `--loop` worker stays behind
  the opt-in `feedback` compose profile and needs its own bot token.
  `source_run_id` (`gha:<run_id>:<attempt>`, `worker`, `local`) on every audit
  row attributes each callback to the run that collected it.
- **Why.** Two pollers on one token race on `getUpdates` offsets and return
  409, so presses are lost or double-counted (see #68).
- **Audit is a separate append-only table** (`feedback_callback_audit`,
  migration 3), not columns on `alert_feedback`: duplicates and rejections
  never reach `alert_feedback`, and an additive table keeps migration risk
  low. Idempotency stays keyed on the `callback_query_id` UNIQUE constraint,
  not `update_id`, because Telegram may restart update ids after a week.
- **Acknowledge before commit.** Telegram only accepts `answerCallbackQuery`
  for a short window, so it is sent right after validation. The text can only
  say "received"; a press later rejected as `unknown_alert` was already
  answered. Accepted trade-off.
- **Timestamps.** Telegram has no press timestamp. `received_at_utc` is the
  getUpdates receipt time and `telegram_message_date_utc` the alert's send
  time; neither is the press time.

## 2026-10-17 — #74: persist merges price history by data, from the master tip
Run 38583134765 lost a whole scrape. It waited in the `monitor` concurrency
queue, and `actions/checkout` (no `ref:`) reset to the SHA the run was
created with (`c5f25b35c`), although master had moved to `9204aec2e`. Persist
committed a full `data/price_history.json` built on that stale base, and
replaying it on the tip hit a textual conflict: both commits appended one
observation to the same 27 product arrays.
- **`ref: master`** on the scrape and persist checkouts only, so a queued run
  scrapes and persists from the current tip. Gate and golden-gate are
  unchanged; they read no data.
- **Merge by data, three ways.** `scripts/persist_history.py` (stdlib, run by
  the bare system Python) fetches the tip, merges with
  `bf_price_monitor/history_merge.py`, commits only `data/price_history.json`
  and pushes a fast-forward, retrying a rejected push up to 3 times from a
  fresh fetch (5 s, 10 s backoff). Per product, an observation survives when
  the tip has it and the incoming run did not remove it, or when the incoming
  run added it. The base is the scrape job's checkout SHA, passed as a job
  output; `--require-base` makes a missing or malformed base exit 2 instead of
  merging. A plain two-way union was rejected: the tip still holds every row
  `prune_history` (90 days, minimum 2) trims, so a union would undo pruning
  on every run once rows age out (about 2026-12-05). Products are a union, as
  the scrape never deletes one. `_merge_product_entries` moved verbatim into
  `history_merge.py` so scrape.py and persist share one implementation.
- **No rebase, reset or force.** Rebasing replays a whole-file edit and
  cannot merge two appends to the same arrays, which is exactly what failed.
  Recomputing the merge on each attempt never rewrites history: an unpushed
  attempt is left behind on a detached HEAD.
- **`data/watchlist.json` is no longer uploaded or committed by CI.**
  scrape.py never writes it (its only use is the read in `main()`), and
  re-committing the scrape job's older copy could revert an edit pushed while
  a run was in flight.
- **Recovery is manual and time-limited.** If every push attempt fails, the
  scraped history exists only in that run's `price-data` artifact, kept for 1
  day (`retention-days: 1`). Re-run the persist job, or download the artifact
  and run `persist_history.py --incoming <dir> --base <sha> --require-base`
  from a clean master checkout, before it expires.
- The `Record base commit` step writes `GITHUB_OUTPUT` with
  `Add-Content -Encoding ascii`, because this job runs Windows PowerShell 5.1,
  whose `>>` writes UTF-16LE. The older fingerprint and cache-check steps use
  `>>` and demonstrably still deliver outputs; they were left alone.
- Verified with local bare-repo tests only (stale-base race, rejected push,
  no-op, bad input). The first real queued run is the live check.

## 2026-10-17 — Dashboard Phase 1 slice A: `enabled: false` is honoured
- **Where:** the filter sits in `scrape._watch_retailers`, the single place
  that turns watches into (watch, retailer) pairs. It also covers fan-out
  (`site: "all"`) watches.
- **Why there and not later in `_run`:** a shop enters `per_store`, and so
  the health log, only through a yielded pair. Skipping disabled watches
  before that point means a shop whose watches are all disabled writes no
  health record at all. It therefore can never collect the two zero-product
  records that quarantine it and raise a false "SCRAPER BREAKDOWN". Skipping
  after `per_store.setdefault` would have caused exactly that alert.
- **notify.py:** the cooldown loader and the channel loader skip disabled
  watches too. Otherwise a paused watch with a short `cooldown_hours` would
  still tighten the cooldown of the live watches on the same site.
- **Unchanged:** qa_watchlist already filtered disabled watches, and
  report.py still lists them labelled "disabled".
- **Trade-off:** while a shop is fully disabled it emits no health signal,
  so a broken scraper for that shop goes unnoticed until it is re-enabled.
  That is intended. A "paused" health record type is Phase 2 material
  (docs/design/retailers-config.md §6.2).

## 2026-10-17 — Dashboard Phase 1 slice B: core library (`bf_price_monitor/dashboard/`)
- **Edits are operations, not file replacements.** `add`, `update`,
  `set_enabled` and `delete` each carry the watch exactly as the user saw it.
  Applied if the current watch equals it; a no-op if the current watch already
  equals the result (a replay whose push landed earlier); otherwise a
  `Conflict`. An unrelated bot commit therefore never blocks an edit, and a
  same-watch race is never silently overwritten.
- **Hidden fields pass through.** Edits patch the current watch dict and never
  rebuild it from form data, so `cadence_minutes`, `owner`, `atl_policy`,
  `track_all_time_low`, `seller_policy` and so on survive every edit (pinned by
  a test). The form does not offer `cadence_minutes`: nothing reads it, the
  schedule is cron. Site and query are fixed after creation (the id is derived
  from them).
- **Unknown keys:** the pure editor keeps them, but the watchlist schema has
  `additionalProperties: false`, so `store.save` refuses a document that has
  one rather than dropping it. A file with an unknown key is already invalid
  for the bot's own validator.
- **Journal under `.git/bf-dashboard/`.** Every op is fsynced there before any
  git work. Each attempt fetches, detaches at origin/master (as
  `persist_history.py` does), replays the journal and pushes, so an unpushed
  commit is never relied on; the journal re-derives it. A replayed op that
  conflicts moves to `conflicts.jsonl`, with every later op on the same watch.
- **Failure states:** `pushed | not_pushed | rejected | conflict | invalid`,
  with a reason (auth, network, timeout, rejected) so the UI can say what to do.
  A rejected push is retried three times (1 s, 2 s); other failures are not.
- **Safety:** every git call has a timeout (30 s network, 10 s local) and runs
  with `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never`. A thread lock plus
  an OS byte lock serialize writers; a second process fails fast ("clone busy").
  No rebase, hard reset, force or branch-creating git verb appears in the
  package (a test scans the sources).
- **Not done yet:** the HTTP server and page (slice C). Nothing imports this
  package. The Windows `msvcrt` lock path is exercised locally only; CI runs
  Ubuntu and uses `fcntl`.

## 2026-10-18 — #76: the base SHA travels as an artifact file, never a job output
- **What failed:** on a tip whose SHA contains a registered secret's value,
  GitHub skips the scrape job's `base_sha` output ("Skip output 'base_sha'
  since it may contain secret", run 39065591612). Persist's `BASE_SHA` was
  empty, Windows PowerShell 5.1 dropped the empty quoted `--base ""`
  argument, and argparse exited 1 with "expected one argument" before
  `persist_history.py` could print its own exit-2 message. A failed persist
  never commits, so the tip stayed put and every real scrape run on it
  failed (#343, #345 on 0516d39; runs on later tips passed).
- **Rule:** never pass commit hashes (or any value that could contain a
  secret substring) through job outputs. Pass them as files inside the
  artifact. The scrape job writes `data/base_sha.txt` (validated as 40 hex,
  ASCII, no newline) and `persist_history.py --base-file` reads it.
  `--base` still works and is what manual recovery from an artifact uses:
  `--base <sha> --require-base`.
- **Fail-closed stays:** a missing, empty or non-hex file exits 2 naming the
  file; `--require-base` still forbids the two-way fallback. An empty value
  that must reach a command line should use the attached form (`--base=`).
- **Artifact retention** for `price-data` goes from 1 to 3 days, so a future
  persist failure leaves time to recover. The data is already public in the
  repository history.
- **Other job outputs audited, unchanged:** `gate.should_run` (true/false),
  `restore_status` and `save_status` (status words). None is a hash.
- **Unverified:** that a short secret (for example SMTP_PORT=587) is the
  substring; secret values are hidden. The mechanism (skipped output, empty
  env, argparse failure) is verified in the run log.
- **Recovery of the lost rows is independent of this fix.** The artifacts
  of failed runs #343 and #345 can be merged with `--base
  0516d39f2cc46aec9a8e6b302951f8587eff2c6f --require-base` whether or not
  this fix is live. They have 1-day retention: the earliest (#343, started
  about 17:59 UTC on 17 Oct) expires about 18:00 UTC on 18 Oct.

## 2026-10-19 - #79: quarantined stores are re-probed

- **Why the old rule could never clear.** Quarantine is a query over
  `scrape_health.jsonl`: the last two records of a store both parsed zero
  while another store succeeded. A quarantined store was skipped before any
  fetch and excluded from the new records, so it wrote nothing. The evidence
  the query reads could never change, and eMAG stayed skipped from
  2026-10-09.
- **Decision.** Quarantine stays a query (the T-09 decision; no stored
  flag). A quarantined store whose newest record is at least
  `QUARANTINE_PROBE_HOURS` (6) old is fetched normally and writes a record
  (`_probe_due_stores`, clock injected as `now_utc`). A probe's own record,
  zero or not, restarts the 6-hour clock.
- **Release and flapping.** Any non-zero probe (`products_parsed > 0`)
  releases the store at once, even a single product; there is no
  confirmation run. A store that then fails again needs two new zero
  records to be quarantined again, so a flapping site can cost two failed
  runs per cycle of release and re-quarantine. Accepted: the cost is two
  extra fetches, and a stricter release rule would delay recovery.
- **Alerts.** Release sends one RECOVERED alert through
  `scrape_health_alerts.json`. A zero probe sends no BREAKDOWN (the store
  was already quarantined) and no DEAD-MAN (suppressed for a probed store
  that stays quarantined, or it would repeat every probe).
- **Unique text.** `notify._health_event_id` hashes the message and a SENT
  hash is skipped, so the RECOVERED text carries the run timestamp.
- **Known gap, not fixed here.** The same hash rule means a second
  BREAKDOWN with identical text for the same store would be suppressed
  after the first was SENT. Needs its own issue.
- **Tracking.** The BREAKDOWN/DEAD-MAN dedupe gap is tracked in #80.
- **Not changed.** Runtime-state files, `report.py`, `notify.py`.

### 2026-10-19 - #79 live verification
- **Exercised live.** Attempt 2 of run 39109621017 (a manual run from
  2026-10-18 07:57Z, re-run at 23:23Z on master) logged
  `[emag] quarantine probe: newest health record is old, fetching` at
  23:27:43Z and parsed 51 eMAG products. Attempt 1 of the same run
  (07:59Z) had logged `[emag] skipped: quarantined`. The scheduled run at
  23:51Z then fetched eMAG normally (49 products, no probe line, as
  expected once released). The probe's persist produced bot commit
  1e46f40 (+494 lines; the later one adds 472), against 162 lines per
  commit while eMAG was quarantined.
- **RECOVERED delivery NOT observed.** RECOVERED delivery was not
  observed: the log grep was inconclusive and Telegram was not checked.
  The re-run likely inherited send_test_alert (unconfirmed). Confirm on
  the next real recovery.
- **Re-runs keep the original `createdAt`.** In `gh run list` a re-run
  shows the first attempt's creation time (07:57Z), not when it ran
  (23:23Z). Use `gh api .../runs/<id>/attempts/N` for the real times. This
  hid the run that produced 1e46f40 from the list.
- **17 Oct hypothesis refuted.** The guess that an unrestored-state run on
  17 Oct had cleared the quarantine is wrong: the health file has no eMAG
  rows between 2026-10-09 03:16Z (zero) and 2026-10-18 23:26Z (51).

### 2026-10-19 - #80: health-alert text identifies the outage
- **Problem.** `notify._health_event_id` hashes the alert text and skips a
  hash that was already SENT. BREAKDOWN and DEAD-MAN used fixed text, so a
  second outage of the same store (now possible after #79) would send
  nothing.
- **Decision.** Put the outage identity in the text, not in notify.py.
  BREAKDOWN appends `(outage since <run_started_utc of the first record of
  the trailing zero streak>)` via `_outage_start`. DEAD-MAN appends each
  stale store's `last_known_good_utc`. Both are stable while an outage
  lasts (repeats still dedupe, a failed probe extends the same streak) and
  change after a recovery, so a new outage alerts once.
- **Why not change notify.py.** The dedupe is deliberate crash-replay
  protection; the producer knows what an outage is, the sender does not.
- **Existing text prefixes are unchanged**, so report and analyze readers
  that match on `SCRAPER BREAKDOWN` / `DEAD-MAN CHECK` keep working.

## 2026-10-20 — Dashboard Phase 1 slice C: HTTP layer over the slice B save engine
- **Slice B's `GitSync` is the only write path.** The Trap 2 sequence in the
  design doc (ff-only or merge, `merge --abort`, blob SHA gate) is superseded:
  journal replay on a detached HEAD never merges, so there is nothing to
  abort, and a lost push is recovered from the journal rather than a kept
  local commit. The design doc now says so under Trap 2.
- **409 means a per-watch conflict.** It comes from the expected-value check in
  `operations.apply`. The `etag` (sha256 of the file bytes) is returned for the
  page's benefit and never gates a write: the bot no longer commits
  `watchlist.json`, so a changed blob usually means an unrelated edit.
- **Clone state:** clean, and detached or on `master`, checked at startup and
  before every write. Slice B detaches the clone on purpose.
- **Form fields are exactly what `operations.py` can change:** add (site,
  query, target_price, min_drop_percent, channels), edit (target_price,
  min_drop_percent, channels), enable or disable, remove. Everything else is
  shown read-only; `EDITABLE_FIELDS` is not extended. Constraints for a later
  slice: `seller_policy` any|trusted, `cooldown_hours` integer >= 1,
  `cadence_minutes` 5..1440, `drop_rule` percentage|absolute,
  `drop_threshold` > 0. `atl_policy` has no schema constraint (`{}`), so the
  analyzer's use of it must be read before an editor is proposed.
- **Local-server guards (S11):** `127.0.0.1` only, exact `Host` check including
  the port, random per-launch token in a header on every write, JSON bodies
  only (415 otherwise), no CORS headers, no CDN assets. The token is printed
  to stdout once and never logged.
- **One slice B change was made in slice C:** `GitSync._sync` no longer fails
  when `git rev-parse HEAD` errors after a successful push. The push has
  landed and the journal entry is cleared; the result is `pushed` with an
  unknown head, so the server answers 200 instead of a misleading 500.

## 2026-10-20 — Dashboard token kept per tab in sessionStorage
- **Decision:** the page saves the per-launch token in `sessionStorage`
  (key `bf-dashboard-token`) after reading it from the `#token=` fragment or
  the paste box, and reads it back after a reload. Any write that returns 403
  clears the saved and in-memory token, shows the token box and one banner
  ("Token missing or wrong. Paste the token from the terminal."); the request
  is not retried. If storage is unavailable the in-memory variable is used.
- **Why sessionStorage:** it survives a reload, is scoped to the one tab, and
  ends when the tab closes. A restart of the server makes the old token
  invalid, and the 403 handling then clears it.
- **Residual risk:** a script running on the page could read it. The strict
  CSP (no inline or external script), text-only rendering, loopback binding
  and a token that changes on every launch keep that risk small.
- **Rejected:** in-memory only (lost on every reload, the problem being fixed);
  `localStorage` (outlives the tab and the server, shared by all tabs);
  keeping the fragment in the URL or history (leaks into history and
  screenshots); an HttpOnly cookie (needs a CSRF design the header token
  currently avoids).

## 2026-10-23 - Dashboard: a caller's own refused edit is reported once; slice B changes made later
- **Decision:** when `GitSync.submit` replays the caller's own operation and
  it is refused as `invalid` (for example a duplicate add), the entry that
  replay parked in `conflicts.jsonl` is dismissed in `_finish`. The caller
  already gets the refusal in the reply (HTTP 422), so a second notice under
  "Edits that could not be applied" looked like a lost edit (QA finding F-01).
  Pinned by `test_submit_reports_its_own_refusal_once_and_does_not_park_it`
  (fails without the dismissal, passes with it) and by the browser test for
  TC-09.
- **Why here and not in `_park`:** `_park` also serves `retry()` and other
  pending operations, whose owner is no longer waiting for a reply. Those
  must stay in the conflicts list, so only the focused operation of a
  `submit` is dismissed.
- **Not covered:** the same double report still happens for a *conflict* (409)
  of the caller's own operation: red banner plus a conflicts-list entry
  (QA finding F-07, TC-26). A conflict means somebody else changed the
  watch, so keeping it listed is arguably right; left open on purpose.
- **Changes to slice B files made after slice B (`git diff 8877c5f..HEAD`):**
  1. `sync.py`, 1e1aa8c (made in slice C, recorded above): a failed
     `git rev-parse HEAD` after a successful push gives `pushed` with an
     unknown head instead of an error.
  2. `sync.py`, 45b4867: the dismissal described above.
  3. `operations.py`, 45b4867: `_check_price` now says
     `... must be greater than 0 and at most 10,000,000`. Before, a price
     above the limit was refused with "must be greater than 0", which was
     false (QA finding F-03). Message only; the accepted range is unchanged.
  No other slice B file (`journal.py`, `store.py`, ...) has changed.

## 2026-10-28 — BF peak window set to 5-30 Nov 2026
The owner set `BF_PEAK_START_UTC` = `2026-11-05T18:00:00+00:00` and
`BF_PEAK_END_UTC` = `2026-11-30T21:59:00+00:00` as repository variables
(read back with `gh variable list` on 2026-10-28). They replace the
Thursday 26 to Friday 27 Nov window that the docs used as an example. The
stores run campaigns from late October to late November, so one Black
Friday day was the wrong unit for the 30-minute cadence. Black Friday itself
is still Friday 27 Nov 2026.
- One window, because `scripts/cadence_gate.py` reads one start and end pair.
  The window already runs to 30 Nov 23:59 local, so Flanco's last day is
  inside it and no second window is configured.
- The gate stays fail-closed. The 30-minute cadence runs only when both
  variables are set and valid, and the cadence stays at 2 hours before 5 Nov.
- Repository variables, not secrets, are what `monitor.yml` reads. Two
  secrets with the same names (created 2026-09-28) are unused and stay until
  the owner deletes them.
- Campaign dates are owner-supplied, from press reports. Not checked by the
  assistant against store pages. Recorded in
  `docs/runbooks/BF_WEEK_CADENCE.md`, section 1.
- Known gap: the 30-minute cadence does not cover Flanco, Altex or evoMAG
  before 5 Nov. Those runs stay at the 2-hour cadence.
