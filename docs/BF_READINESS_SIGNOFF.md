# Black Friday Go/No-Go Readiness Signoff (T-31, #42)

Status: **rehearsal executed** (2026-09-28). One manual production run was
triggered and measured; the matrix below combines that run with earlier
scheduled-run evidence and owner-confirmed channel evidence. NOT YET
MEASURED means no evidence exists, not negative evidence. Owner sign-off
boxes are left for the owner.

**Result: NO-GO as of 2026-09-28.** The core pipeline passed end to end,
but two gates fail (6: Healthchecks.io ping, 10: cross-run state). Both
disable the monitor's own failure detection. Neither is fixed here.

## Scope and deviations from the #42 blueprint

- **No shadow mode, no staging channel.** The blueprint asks for a shadow
  run routed to a private staging Telegram channel. None exists, and
  building one is out of scope. The rehearsal is a normal production run
  triggered manually (`workflow_dispatch`); any alert it produces goes to
  the real channels.
- **Teams is not a channel.** Removed by decision (docs/DECISIONS.md,
  2026-09-26). Live channels are Telegram, email and ntfy.
- **Watchlist size.** The blueprint's latency target assumes 50 watchlist
  items; `data/watchlist.json` has 4 watches (eMAG x2, PC Garage, Flanco).
  Latency is measured against what is actually configured.
- **Altex** is a placeholder scraper (Akamai-protected, accepted gap) and is
  excluded from extractor-health gates.
- **"Runner cleanup"** is a step inside the `scrape-analyze-notify` job, not
  a separate job.
- **Dry run vs scheduled run.** `monitor.yml` has no step conditioned on the
  trigger event, so a manual dispatch runs exactly the same jobs, steps,
  secrets and side effects as a scheduled run.
- **Alerts cannot be forced.** The watchlist, prices, cooldowns, channels
  and secrets were not changed to provoke a notification, and the run was
  not repeated to fish for one.

## Rehearsal run

- Run: [36363659365](https://github.com/NaviAndrei/bf-price-monitor/actions/runs/36363659365),
  `workflow_dispatch` on master at 5eab5ff (first production run of the
  T-40 fan-out and placeholder-exclusion code).
- Result: **success**. Created 2026-09-28 00:50:03 UTC, finished
  00:51:57 UTC: **1 min 54 s** wall time.
- Job `scrape-analyze-notify` (00:50:07 to 00:51:29): all steps success;
  dependency and Playwright install skipped (runner cache hit).
- Job `persist` (00:51:31 to 00:51:57): all steps success, including
  `Ping healthcheck`, whose log nevertheless reads
  `WARNING: [healthcheck] ping failed or returned a non-success status; monitor job remains successful`.
- Scrape: `Checked 4 watchlist entries, 0 price change(s) detected`.
  Analyze: `No alerts to analyze`. Notify: `No alerts to send`.
  **No alert was produced, so no notification was sent by this run.**
- Altex: zero mentions in the run log (placeholder excluded from fan-out as
  intended).
- Runner cleanup: terminated 8 leftover `chrome` processes, found no stale
  temp items, left the runner cache untouched.
- Persist commit: [1e34d74](https://github.com/NaviAndrei/bf-price-monitor/commit/1e34d74b9ec31bc61fa29d956df096e0cb1a9e78)
  `chore: update price history [skip ci]` (data/price_history.json, +523
  / -1), pushed `5eab5ff..1e34d74`.
- Artifacts: `price-data` (id 10946830649, 52,136 bytes, expires
  2026-09-29) and `price-report` (id 10946960418, 36,763 bytes zipped,
  expires 2026-10-05).

### Per-retailer health (rehearsal `data/scrape_health.jsonl`)

| Store | watches_requested | products_parsed | matched_count | parse_failures | challenge_detected | challenge_wait_entered | latency_seconds | last_known_good_utc |
|-------|------|------|------|------|------|------|------|------|
| emag | 2 | 56 | 0 | 0 | false | false | 6.079 | 2026-09-28T00:50:29Z |
| pcgarage | 1 | 20 | 0 | 0 | false | false | 9.907 | 2026-09-28T00:50:29Z |
| flanco | 1 | 11 | 0 | 1 | false | false | 2.922 | 2026-09-28T00:50:29Z |

`matched_count` counts products that met a watch's alert rule
(`should_alert`) and were not blocked by seller policy, so 0 on every store
means no alert candidates this run, consistent with `0 price change(s)`.
It is not a count of products found; the report's watches section lists
products for all 4 watches. The Flanco parse
failure is the same product as in earlier runs, logged as
`AI extraction not implemented (query='laptop asus vivobook', ...)`.

## Other evidence sources

- Scheduled runs of `monitor.yml`, 2026-09-25 to 2026-09-27: the 14 most
  recent successful runs before the rehearsal, latest [36355889913](https://github.com/NaviAndrei/bf-price-monitor/actions/runs/36355889913)
  (2026-09-27 22:36 UTC, commit af96f48).
- `price-data` artifacts from the 6 scheduled runs whose 1-day retention
  had not expired (36290489672 through 36355889913).
- `price-report` artifact of run 36355889913.
- Quality Gate run [36359483159](https://github.com/NaviAndrei/bf-price-monitor/actions/runs/36359483159) on 5eab5ff.
- Owner confirmation (2026-09-28) of the Telegram alert sent by run
  36355889913 around 2026-09-27 22:38 UTC: PC Garage, ASUS Vivobook Go 15
  E1504TA, 1,464.10 RON.

## Decision matrix

| # | Gate | Criterion | Evidence | Status | Owner sign-off |
|---|------|-----------|----------|--------|----------------|
| 1 | Rehearsal run | Manual run on the production self-hosted runner with the real watchlist completes | Run 36363659365: conclusion `success` | PASS | [ ] |
| 2a | Scrape | Step succeeds, all live retailers return products | Run 36363659365, step `Scrape prices`; health table above | PASS | [ ] |
| 2b | Analyze | Step succeeds | Run 36363659365, step `Analyze alerts` (`No alerts to analyze`) | PASS | [ ] |
| 2c | Notify | Step succeeds | Run 36363659365, step `Send alerts` (`No alerts to send`); success here proves no delivery, see gates 5 and 11 | PASS | [ ] |
| 2d | Persist | Updated history committed and pushed | Run 36363659365, job `persist`, step `Commit updated price history`; commit 1e34d74 | PASS | [ ] |
| 2e | Report build | HTML report written | Run 36363659365, step `Build HTML report`: `Wrote report\index.html (386,583 bytes, 0 alert(s), 306 product(s))` | PASS | [ ] |
| 2f | Report upload | `price-report` artifact present | Artifact `price-report` id 10946960418 on run 36363659365 | PASS | [ ] |
| 2g | Runner cleanup | Step succeeds without touching the runner cache | Run 36363659365, step `Runner cleanup`: `Cleanup complete.`, cache `not touched` | PASS | [ ] |
| 3a | Extractor health: eMAG | Products parsed, no bot challenge | Rehearsal: 56 parsed, 0 failures, no challenge; 6 earlier health artifacts: 54 to 57 parsed, no challenge | PASS | [ ] |
| 3b | Extractor health: PC Garage | Same | Rehearsal: 20 parsed, 0 failures, no challenge; same in 6 earlier artifacts | PASS | [ ] |
| 3c | Extractor health: Flanco | Same | Rehearsal: 11 parsed, no challenge; 1 product-level parse failure, recurring in all 7 measured runs (listing selectors work; one product falls through to the unimplemented AI fallback) | PASS | [ ] |
| 4a | Latency, 4 watches | Whole workflow under 8 minutes | Run 36363659365: 1 min 54 s; 14 earlier runs 1.5 to 2.5 min | PASS | [ ] |
| 4b | Latency, 50 watches | Blueprint's scale target under 8 minutes | Not measurable: the watchlist has 4 watches | NOT YET MEASURED | [ ] |
| 5 | Alert delivery latency | Every generated alert delivered (SENT) within 30 s | No alert in the rehearsal; notify.py logs nothing on success and `data/alert_outbox.jsonl` is not kept (gate 10); no reliable timestamped source | NOT YET MEASURED | [ ] |
| 6 | Healthchecks.io ping | Dead-man switch receives a successful ping each run | 15 of 15 runs (14 scheduled plus rehearsal 36363659365, job `persist`, step `Ping healthcheck`) log the ping-failed warning while the step stays green | **FAIL** | [ ] |
| 7 | Action pinning | Zero unpinned `uses:` references | All 8 `uses:` steps (6 in monitor.yml, 2 in quality.yml) pinned to 40-char SHAs, 0 unpinned; Quality Gate "Enforce SHA-pinned Actions" passed on 36359483159 | PASS | [ ] |
| 8 | Credential hygiene | Zero credentials in logs or repo | Rehearsal log and 14 earlier logs pattern-scanned (Telegram bot token and API URL, hc-ping URL, ntfy topic URL, HF token, password assignments): 0 hits, secrets appear only as `***`; Quality Gate Gitleaks passed. Pattern scan, not exhaustive | PASS | [ ] |
| 9 | Report artifact | Self-contained, all three sections | Rehearsal `price-report`: sections `Recent alerts (0)`, `Watches (4)`, `Store health`; 0 `<script>`, 0 `<link>`, 0 remote `src=`, 0 remote CSS `url()`, 0 iframes; the 306 external URLs are plain product `<a href>` links; all 4 watch queries present | PASS | [ ] |
| 10 | Cross-run state | Runner-local state survives between runs | Rehearsal job `persist`, checkout step logs `Removing data/price_history.db`, `data/scrape_health.jsonl`, `data/scrape_health_alerts.json` (and others); job `scrape-analyze-notify` checkout logs `Removing data/scrape_health.jsonl`; run 36355889913 also logged `Removing data/alert_outbox.jsonl` | **FAIL** | [ ] |
| 11a | Telegram delivery and rendering | Alert arrives and renders correctly | Owner-confirmed: alert from run 36355889913 (~2026-09-27 22:38 UTC) arrived with chart, title, retailer, seller, stock, current/prior/30-day prices, `REDUCERE FALSĂ` badge, Romanian explanation, offer and comparison links. One alert only | PASS | [ ] |
| 11b | Email delivery and rendering | Same | Only the eMAG "laptop lenovo v15" watch routes to email; it has produced no alert in the evidence window, and the rehearsal produced none | NOT YET MEASURED | [ ] |
| 11c | ntfy delivery and rendering | Same | Same routing as email; no alert produced | NOT YET MEASURED | [ ] |

## NO-GO blockers

### B1 — Healthchecks.io ping fails on every run, hidden by a green step (gate 6)

Direct evidence:
- 15 of 15 measured runs log
  `WARNING: [healthcheck] ping failed or returned a non-success status; monitor job remains successful`,
  including rehearsal 36363659365 (job `persist`, step `Ping healthcheck`).
- The step wraps `Invoke-WebRequest` in `try/catch` and prints the same
  generic warning for a non-2xx status and for any exception, so the step
  and the run finish `success`.
- The log does not record the exception type, HTTP status or whether the
  request left the runner, so the cause cannot be read from it.

Inferred impact (not observed on the healthchecks.io side, which was not
checked):
- The external dead-man switch has not received a successful ping from
  these runs, so it is either alarming continuously or muted; in either
  case it would not signal a real outage.
- GitHub Actions `success` is not evidence that the dead-man switch works.

### B2 — Checkout wipes runner-local state every run (gate 10)

Direct evidence (log lines):
- Both jobs run in one shared workspace on the self-hosted runner, and
  `actions/checkout` removes untracked and ignored files there.
- Rehearsal, job `persist` checkout: `Removing data/price_history.db`,
  `Removing data/scrape_health.jsonl`,
  `Removing data/scrape_health_alerts.json`, plus `data/alerts.json`,
  `data/extraction_failures.jsonl`, `data/formatted_alerts.json`.
- Rehearsal, job `scrape-analyze-notify` checkout:
  `Removing data/scrape_health.jsonl` (the copy the previous run's
  `persist` job restored from the artifact).
- Scheduled run 36355889913, job `persist` checkout also logged
  `Removing data/alert_outbox.jsonl` (that run sent an alert; the
  rehearsal sent none, so no outbox existed to remove).
- Every measured health artifact contains only its own run's three
  records, never earlier history.

Inferred impact (from reading scripts/scrape.py and scripts/notify.py; not
observed failing in production):
- SQLite history (`data/price_history.db`) is rebuilt from nothing each
  run, contrary to the 2026-09-23 decision that treats it as persistent.
- The cooldown between runs and PENDING-alert replay read
  `data/alert_outbox.jsonl`, so they start empty each run.
- The in-app dead-man check compares against the previous run's
  `last_known_good_utc`, which never exists.
- The Critical Selector Drift quarantine and "SCRAPER BREAKDOWN" alert need
  at least two runs of health history, so they cannot fire.
- `data/price_history.json` is unaffected (tracked and committed by
  `persist`).

Together, B1 and B2 leave the monitor without working self-monitoring: a
silent scraper failure would be caught neither externally nor in-app.

## Checked and not a finding

The alert of run 36355889913 (PC Garage, ASUS Vivobook Go 15 E1504TA,
1,598.99 to 1,464.10 RON) is labelled FALSE_DISCOUNT with a 30-day minimum
of 1,464.10 RON. The price history shows 1,464.10 on 2026-09-07 and
2026-09-08, inside the window, so genuine savings against the 30-day
reference are 0% and the verdict is correct under the Omnibus rule. The
owner confirmed the same result from the delivered Telegram card.

## Recommendation

NO-GO until B1 and B2 are resolved and re-verified on real runs. What the
evidence supports as ready: scraping of eMAG, PC Garage and Flanco, the
analyze and persist stages, the report artifact, action pinning, credential
hygiene, 4-watch latency, and Telegram rendering for one alert.

## Remaining open items

- Gate 4b: latency at 50 watches (watchlist has 4).
- Gate 5: alert delivery latency; needs a timestamped delivery record.
- Gates 11b and 11c: email and ntfy need a real alert on the eMAG
  "laptop lenovo v15" watch and the owner's confirmation.
- `price-data` artifacts expire after 1 day; the rehearsal's expires
  2026-09-29.
