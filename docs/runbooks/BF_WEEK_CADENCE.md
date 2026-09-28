# BF-week scrape cadence & rate-limit safety runbook

Covers how `.github/workflows/monitor.yml` tightens its scrape cadence for
Black Friday week, the request-safety limits that apply on every run, and
what to do if eMAG, PC Garage, or Flanco start blocking us. Written for
T-32 (#43).

## 1. Schedule decision

| Period | Cron | What actually scrapes |
|---|---|---|
| Normal (all year) | `0 */2 * * *` | Every run, all watches |
| BF peak window | `*/30 * * * *` (plus the 2-hour cron) | Every 30 minutes, all watches |

- The 30-minute cron is **always registered** in `monitor.yml`, but a
  lightweight `gate` job (`scripts/cadence_gate.py`) lets it through only
  when the current UTC time falls inside the window defined by two
  repository **variables** (Settings → Secrets and variables → Actions →
  Variables):
  - `BF_PEAK_START_UTC` — ISO 8601 with offset, e.g. `2026-11-26T18:00:00+00:00`
  - `BF_PEAK_END_UTC` — ISO 8601 with offset, e.g. `2026-11-27T21:59:00+00:00`
- Both variables are **unset by default**. With either one unset, unparsable,
  or missing its UTC offset, the gate **fails closed**: the 30-minute cron is
  skipped. The 2-hour cron and `workflow_dispatch` always pass the gate.
- **The BF window is not hardcoded.** eMAG, PC Garage, and Flanco announce
  their Black Friday campaign dates themselves, and not far enough ahead to
  bake into code. Set the variables once the actual dates are announced.
  The blueprint target of "Thursday 20:00 to Friday 23:59" is Romania local
  time, which is UTC+2 in late November. So Thursday 20:00 local is
  `18:00:00+00:00`, and Friday 23:59 local is `21:59:00+00:00`.
- **Scope note:** the blueprint asked for 30 minutes on "high-priority items".
  `data/watchlist.json` has no priority field today, so during the window
  **all** watches run at 30 minutes. With the current watchlist size (4
  watches over 3 stores), that is 8 page loads per hour. The two live trial
  runs in section 3 showed no blocking at that load. Per-item priority
  would be a separate change.
- **Known overlap:** on even hours inside the window, the 2-hour cron and the
  30-minute cron both fire at `:00`. The `monitor` concurrency group queues
  them, so they run one after the other rather than in parallel. Any store
  flagged by the first of those two runs is skipped by the second
  (section 2's cooldown).
- Outside the window, the 30-minute gate check costs one checkout plus one
  Python call on the self-hosted runner (seconds). It never installs
  Playwright or touches a retailer.

## 2. Request-safety limits (always on, every run)

All in `scripts/scrape.py`:

- **Jitter between watches:** `random.uniform(4, 9)` seconds (was 3–7).
- **Per-domain floor:** `_pace_domain()` guarantees at least
  `DOMAIN_PACING_FLOOR_SECONDS = 5.0` between two requests to the same store
  within one run. This includes retries and the browser path.
- **User-agent rotation:** each browser context and each plain HTTP attempt
  picks a UA at random from `CHROME_USER_AGENTS`: Chrome 131 and Chrome 130
  on Windows 10 x64.
- **403/429 tracking:** every fetch attempt increments
  `fetch_attempts`, and every 403/429 increments `rate_limit_hits`. Both are
  written per store into `data/scrape_health.jsonl`. The runtime-state step
  restores and saves this file between runs (B2, #42).
- **Automatic cooldown:** if a store's most recent health record shows
  `rate_limit_hits / fetch_attempts > 5%`, that store is skipped for any run
  starting within **30 minutes** of that record's `run_started_utc`. The
  console shows
  `[<store>] skipped: cooling down after a >5% 403/429 rate ...`, and the
  skipped run writes no new health record. The cooldown is derived from
  history on every run rather than stored as a flag, the same pattern as the
  T-09 Critical Selector Drift quarantine. It clears on its own.
  - **Limitation:** the 30 minutes are counted from the flagged run's
    *start*. GitHub usually starts scheduled runs a few minutes late, so the
    next regular 30-minute run typically lands just after the cooldown has
    expired. In practice, the automatic cooldown blocks the overlapping
    even-hour run and any manual dispatch. It does **not** reliably skip a
    full 30-minute cycle. If a store keeps tripping it, use the rollback
    steps in section 4 rather than relying on the cooldown.
- **Pre-existing, unchanged:** 403/429 retry backoff of 5s / 10s / 20s
  (`RETRY_BACKOFFS`), Cloudflare challenge detection, the Critical Selector
  Drift quarantine, and the dead-man check.

## 3. Trial evidence

Two real, back-to-back runs of `uv run scripts/scrape.py` against all three
live retailers, from the owner's dev machine on 2026-09-28. The runs were
about 105 seconds apart, which is much tighter than the 30-minute peak
cadence. Source: the local `data/scrape_health.jsonl` rows they wrote.

| Run start (UTC) | Store | fetch_attempts | rate_limit_hits | challenge_detected | products_parsed |
|---|---|---|---|---|---|
| 2026-09-28T17:59:44 | emag | 2 | 0 | false | 57 |
| 2026-09-28T17:59:44 | pcgarage | 1 | 0 | false | 20 |
| 2026-09-28T17:59:44 | flanco | 1 | 0 | false | 10 |
| 2026-09-28T18:01:29 | emag | 2 | 0 | false | 56 |
| 2026-09-28T18:01:29 | pcgarage | 1 | 0 | false | 20 |
| 2026-09-28T18:01:29 | flanco | 1 | 0 | false | 10 |

Run IDs: `d1447e2f-7ec2-454c-ba14-86b70b6c8b90`,
`afa78f2a-dcee-408f-a038-6a6c5569ab80`.

**What this does and does not show:**

- It shows that two consecutive full runs close together got zero 403/429
  responses and zero challenge pages on any of the three stores, with the
  new UA rotation and pacing active.
- **NOT YET MEASURED:** a sustained multi-hour 30-minute cadence, behaviour
  from the production runner's IP (`PC-A1208`), and behaviour under BF-week
  anti-bot tightening on the retailers' side. Retailers may harden
  protections during BF itself, so check the section 5 dashboards on the
  first peak day.
- The pre-existing `parse_failures` counts (eMAG 1, Flanco 2) are extraction
  misses, not blocks. They are unrelated to cadence.

## 4. Rollback plan: a site starts blocking

**Signals** (any one of these):

- Console lines `skipped: cooling down after a >5% 403/429 rate` for the
  same store on more than one run.
- `rate_limit_hits > 0` or `challenge_detected: true` in
  `scrape_health.jsonl` for a store across consecutive runs.
- A Critical Selector Drift alert for a store that was healthy the day before.
  Some block pages return 200 with no product markup.

**Steps, in order of escalation:**

1. **Drop back to the 2-hour cadence (instant, no deploy).** Delete, or
   clear, the `BF_PEAK_START_UTC` repository variable. The next 30-minute
   trigger's gate fails closed and skips. No code change, no commit, no
   runner restart needed. This is the primary lever.
   ```bash
   gh variable delete BF_PEAK_START_UTC --repo NaviAndrei/bf-price-monitor
   ```
2. **Shrink the window instead of removing it.** If blocking only happens
   at particular times, edit `BF_PEAK_START_UTC`/`BF_PEAK_END_UTC` to a
   narrower range.
   ```bash
   gh variable set BF_PEAK_END_UTC --body "2026-11-27T12:00:00+00:00" --repo NaviAndrei/bf-price-monitor
   ```
3. **Pause the one blocked store.** Temporarily remove that store's watches
   from `data/watchlist.json` in a normal commit. The other stores keep
   running at the current cadence.
4. **Stop all scheduled scraping.** Disable the workflow and run manually
   with `workflow_dispatch` when needed.
   ```bash
   gh workflow disable monitor.yml --repo NaviAndrei/bf-price-monitor
   ```
   Re-enable with `gh workflow enable monitor.yml --repo NaviAndrei/bf-price-monitor`.
5. **Slow down every run (code change).** Raise
   `DOMAIN_PACING_FLOOR_SECONDS`, the `random.uniform(4, 9)` jitter, or
   `RATE_LIMIT_COOLDOWN_MINUTES` in `scripts/scrape.py`, then commit to
   `master`.

**Recovery:** once a blocked store shows two consecutive clean runs on the
2-hour cadence (`rate_limit_hits: 0`, `challenge_detected: false`), restore
the window variables to re-enable the 30-minute cadence.

## 5. Where to look

- Per-store health: `data/scrape_health.jsonl` on the runner, restored from
  `C:\bf-monitor-runtime-state` at the start of each run.
- Whether the gate let a trigger through: the `gate` job's
  `[cadence_gate] ... -> should_run=<True|False>` log line. When the gate
  says no, `scrape-analyze-notify` and `persist` show as *skipped* rather
  than failed.
- Recent runs:
  ```bash
  gh run list --workflow monitor.yml --repo NaviAndrei/bf-price-monitor --limit 20
  ```
