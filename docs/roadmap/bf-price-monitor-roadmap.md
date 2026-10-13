# bf-price-monitor Roadmap Report (delta snapshot, Sprints 8-11)

> **Status of this document:** derived report, NOT a source of truth. Per `CLAUDE.md`, the roadmap lives in GitHub milestones and issues; `docs/ROADMAP.md` is the synced snapshot. This file is a delta-only view built from the `origin/master` tree (audited at `1206be4`; `e05a38a` differs only in `data/price_history.json`) and the GitHub issue list (63 issues, #2-#69).
>
> **Marks:** `UNVERIFIED` = not read in the audit session, or not reachable from it. "I don't know" is stated where it applies. All IDs below T-47 are shipped work and are not re-planned here. IDs T-48 and up are **proposals**; no GitHub issues were created for them.

## 1. TL;DR

| Sprint | Dates | Goal | Hours (of 16 committed) |
|---|---|---|---|
| 8 | Oct 12-25 | Pre-BF correctness and compliance | 16 |
| 9 | Oct 26-Nov 8 | BF freeze: docs/config only | 8 |
| 10 | Nov 9-22 | Post-peak stabilisation | 11 |
| 11 | Nov 23-Dec 6 | Retro and anomaly pilot | 15 (fallback 7) |

Main-path total: 16 + 8 + 11 + 15 = **50 h**.

## 2. Baseline and method

- Audit checkout was stale (`e527f12`); every finding was re-verified against `origin/master` `1206be4` (142 files differ from the stale checkout).
- `gh` is not installed in the audit environment; issues were read through the GitHub REST API.
- `BF-Price-Monitor-Comprehensive-Enhancement-Roadmap.md` is **not** in the repo; it is cited only in issue bodies (for example #47). `docs/ROADMAP.md` ("Last synced: 2026-09-22") is stale.
- Highest shipped T-number: T-47 (#66), so proposals start at T-48. Hygiene note: T-44 and T-40 are each used twice (#62/#67, #57/#59).
- Not read on purpose: `uv.lock`, `data/` contents (only the size of `data/price_history.json`: 1,845,033 bytes).

## 3. Re-audit of findings F1-F11

Only CONFIRMED findings enter the catalog.

| ID | Verdict | Evidence (path:line, quote) |
|---|---|---|
| F1 | STALE | `analyze.py:427-428` `analysis["verdict"] = rule_verdict`; `["is_recommended"] = rule_verdict == "GENUINE_DEAL"` |
| F2 | STALE | `scrape.py:1487` `observed_at = now_utc.isoformat()`; `:1572` `record_observation(entry, today, observed_at, ...)`; no same-day skip |
| F3 | CONFIRMED (reshaped) | `analyze.py:120` `/ old_price`, `:122` `/ thirty_day_low` unguarded; `:585-592` `try: RawAlertCandidate.model_validate(a)` ... bare `raise` aborts the batch. Whether real data can reach 0: UNVERIFIED (`RawAlertCandidate` constraints not read) |
| F4 | STALE | `notify.py:60` `MAX_ATTEMPTS = 4`; `:64` `MAX_RETRY_AFTER_SECONDS = 60.0`; `:473` bounded loop |
| F5 | MOSTLY STALE | `monitor.yml:37` `permissions: {}`, per-job grants, `persist` job, `golden-gate`, SHA pins. Residual: non-ephemeral self-hosted runner on a public repo (`monitor.yml:4-11`); `version: "latest"` at `monitor.yml:86,148` and `quality.yml:61`; `dependabot.yml` has only `github-actions` and `docker`. `quality.yml:92` second setup-uv: UNVERIFIED |
| F6 | STALE | `scrape.py:311` `context.close()`, `:320` `self._browser.close()`, `:485/:505` `.wait_for(`; remaining `time.sleep` is pacing (`:1548`) |
| F7 | STALE | `monitor.yml:88,181` `uv sync --frozen` |
| F8 | MOSTLY STALE | `sqlite.py:37` `CREATE TABLE IF NOT EXISTS price_observations`. Residual: `data/price_history.json` still committed (`monitor.yml:382`), still read by `notify.py:32` |
| F9 | MOSTLY STALE | `scrape.py:630` `extract_jsonld_products`, `:562` `extract_semantic_price`. Residual: `:676` `extract_with_ai(query, url) -> None` is a stub |
| F10 | STALE | `domain/models.py:15` Watch, `:101` DeliveryAttempt. Doc drift CONFIRMED: `CLAUDE.md:19` says Watch has no site field, "tracked as #56" (closed) |
| F11 | PARTLY CONFIRMED | `docs/progress.md` is not empty (STALE). No LICENSE, CONTRIBUTING or SECURITY at repo root (`git ls-tree`) |

### 3.1 Legal citation (30-day reference price)

- `README.md:52` "legally-mandated 30-day reference price (OUG 27/2022)" and `scripts/scrape.py:1109` "# OUG 27/2022 requires retailers to publish the lowest price".
- `scripts/analyze.py:227` "(Directiva Omnibus / OUG 58/2022)".
- HG 686/2022 and HG 947/2000 appear nowhere in the repo. Occurrences inside `tests/` were not searched: UNVERIFIED.
- The owner states OUG 27/2022 is an energy price cap. **None of OUG 27/2022, OUG 58/2022, HG 686/2022 or HG 947/2000 is verified against primary text** (legislatie.just.ro was not reachable from the audit environment). OG 99/2000: UNVERIFIED, not cited.
- Decision: T-50 touches only the two "OUG 27/2022" locations after the owner confirms the citation. `analyze.py:227` is left alone until the owner confirms all three texts.

### 3.2 Scope of `thirty_day_low`

- Per retailer product, keyed by canonical URL (`scrape.py:1554-1555`); window taken from that entry's prior history (`:1656-1661`). **Not per sales channel**: `seller` / `is_marketplace` are stored but unused for the window, and the eMAG seller is None (`DECISIONS.md:93-98`).
- Empty window falls back to the previous price (`min(recent_prices) if recent_prices else prev_price`). `analyze.py` gates `history_days < 14` to `INSUFFICIENT_HISTORY`.

### 3.3 BF dates

- Sprint 7 milestone is due 2026-10-30; `docs/watchlist-qa.md` uses 2026-11-27; the retro generator default window is 2026-11-23..12-01 (taken from `docs/progress.md`; script line not re-read: UNVERIFIED).
- The BF-week cadence is inert until `BF_PEAK_START_UTC` / `BF_PEAK_END_UTC` are set (`scripts/cadence_gate.py:11-16` "Deliberately fails closed"). **Current variable values: I don't know.**

## 4. Catalog

### 4.1 Done (evidence)

| Scope | Evidence |
|---|---|
| Epics #2-#8, #41; T-01..T-35 (#9-#46), T-37/37b #48/#55, T-38 #49, follow-ups #54, #56-#69 | all closed on GitHub |
| T-23 deterministic invariant | #30, `analyze.py:418-428` |
| T-24 golden gate | #32 |
| T-21 | #29, commits 1b25f7f / 5cce44b, run 36128084436 (40 s vs 42 s) |
| T-32 BF-week cadence | #43, `scripts/cadence_gate.py` |
| T-31 readiness sign-off | #42, run 36363659365 (B1/B2 resolved, owner boxes unchecked) |
| T-30 Docker mode | #38, `Dockerfile`, `docker.yml` |
| T-26 email + ntfy | #35 |
| Tests | 680 passed / 1 skipped (`docs/progress.md`, 2026-09-30) |

### 4.2 Open

| # | Item | State | Blocker |
|---|---|---|---|
| #36 (T-29) | anomaly pilot | tooling built; `blocked_insufficient_labels`, 0 labels, 4,272 observations (`docs/anomaly-pilot.md`) | at least 30 real labels (`docs/feedback-labels.md:15`) |
| #37 (T-28) | feedback labels | stored=5 (run 36542858539), stored=2 (36555784061); GitHub OPEN while `progress.md` says closed | owner closes |
| #40 | epic Q4 | children #36, #47 open | closes with them |
| #47 (T-36) | post-BF retro | `generate_retro_metrics.py` and template exist | real event data after the window |

Owner gates with no issue: `docs/BF_READINESS_SIGNOFF.md` sign-off boxes unchecked; gate 4b (50-watch latency) unmeasured; no shadow channel.

### 4.3 Proposed

Prio = Value / hours (S=2, M=5, L=10). Risk is the tie-break.

| ID | Title | Evidence | V | Eff | Risk | Prio | Deps |
|---|---|---|---|---|---|---|---|
| T-48 | One wide BF window 2026-11-02..2026-12-01 UTC; align retro default, QA window and runbook; record the extra-scrape trade-off in DECISIONS | `cadence_gate.py:32-56`; `monitor.yml:58-59`; `docs/watchlist-qa.md` | 5 | M | M | 1.0 | owner sets repo variables |
| T-50 | Replace "OUG 27/2022" only (`README.md:52`, `scrape.py:1109`); `analyze.py:227` untouched | lines above | 4 | S | L | 2.0 | owner confirms HG 686/2022, HG 947/2000, OUG 58/2022 |
| T-51 | Docs + test: reference price = lowest price in the prior 30 days for the same product on the same sales surface (multi-channel: the channel where the discount is announced); under-30-days case; eMAG channel not derivable; no channel scoping built | `scrape.py:1554-1661`; `analyze.py` (<14 d gate); `DECISIONS.md:93-98` | 4 | S | L | 2.0 | none |
| T-52 | Pin the setup-uv `version` (proposal `0.12.23`, as at `Dockerfile:10`) | `monitor.yml:86,148`; `quality.yml:61` (+ `:92` UNVERIFIED) | 3 | S | L | 1.5 | confirm version against CI / lock |
| T-49 | Guard zero / None `old_price` and `thirty_day_low`; quarantine the bad alert, keep the batch | `analyze.py:120-122`, `:585-592` | 4 | M | M | 0.8 | none |
| T-56 | Record robots.txt / ToS status for eMAG, PC Garage, Flanco | not fetchable from the audit environment | 4 | S | L | 2.0 | owner pastes curl output |
| T-54 | Add LICENSE, CONTRIBUTING, SECURITY | repo root | 3 | S | L | 1.5 | owner picks license |
| T-60 | DECISIONS entry: accepted runner risk, mitigations, re-open triggers (no ephemeral runner) | `monitor.yml:4-11` | 3 | S | L | 1.5 | none |
| T-57 | Decide the fate of tracked `price_history.json` | 1,845,033 B; `notify.py:32` | 3 | S | L | 1.5 | none |
| T-53 | Dependabot `package-ecosystem: "uv"`; first PR must update `pyproject.toml` and `uv.lock`, otherwise record the gap in DECISIONS.md | `.github/dependabot.yml` | 3 | S | L | 1.5 | uv ecosystem support limits: UNVERIFIED |
| T-61 | Two-window support in `cadence_gate.py` so the RO Nov 6 and global Nov 27 peaks need not share one wide window | `cadence_gate.py:32-56` (one start/end pair) | 2 | S | M | 1.0 | T-48; update `docs/runbooks/BF_WEEK_CADENCE.md` |
| T-55 | Resync `docs/ROADMAP.md` (last synced 2026-09-22) and `CLAUDE.md:19` | those lines | 2 | S | L | 1.0 | none |
| T-59 | Spec only for the AI extraction tier (Tier 4, non-authoritative) | `scrape.py:676` | 2 | S | M | 1.0 | #67 health logs |
| T-58 | Implement the T-57 decision (chart history to SQLite, prune JSON) | `notify.py:32` | 3 | M | M | 0.6 | T-57 |

**Not building:** Altex internal API, proxy rotation (ToS / Akamai), forecasting, GitHub Pages dashboard, ephemeral runner, quiet hours / digest.

**T-48 trade-off (recorded):** one 30-day window lets the `*/30` cron fire up to 48 times a day: 30 x 48 = 1,440 scrape attempts, against 360 at the 2-hourly cadence for the same 30 days. GitHub's actual firing rate is lower (`docs/progress.md`); the exact rate: I don't know. This means more load and block risk on three retailers, including between the two peaks (Nov 7-25 is off-peak). T-61 removes the cost later.

## 5. Conflicts and resolutions

| # | Conflict | Resolution |
|---|---|---|
| 1 | GitHub Pages dashboard | Dropped. `monitor.yml:318-319`: report is "published ONLY as a short-lived artifact, never GitHub Pages" |
| 2 | Anomaly / forecasting before labels | Forecasting dropped; anomaly stays as #36, gated on at least 30 labels |
| 3 | Ephemeral runner vs Docker | No ephemeral runner. Docker is an optional always-on mode (`docs/docker.md`), not CI isolation. Risk accepted via T-60 |
| 4 | Duplicates of DECISIONS.md | Removed: quiet hours / digest (T-41 cooldown / dedup, `DECISIONS.md:71`), cadence (`:681`), runtime state (`:456`), Teams (`:427`) |
| 5 | Name clash | Delta sprints are named Sprint 8-11 |

## 6. Sprint plan

Capacity: 2-week sprints of 20 h, 20% buffer, so **16 h committed**. Sizing S=2, M=5, L=10. At most one L per sprint.

| Sprint | Dates | Items | Arithmetic | Hours | L |
|---|---|---|---|---|---|
| 8 | Oct 12-25 | T-48 M, T-49 M, T-50 S, T-51 S, T-52 S | 5+5+2+2+2 | 16 | 0 |
| 9 | Oct 26-Nov 8 | T-56 S, T-54 S, T-60 S, T-57 S | 2+2+2+2 | 8 | 0 |
| 10 | Nov 9-22 | T-58 M, T-61 S, T-53 S, T-55 S | 5+2+2+2 | 11 | 0 |
| 11 | Nov 23-Dec 6 | #47 M, #36 L (only if at least 30 labels) | 5+10 | 15 | 1 |

- Sprint 11 fallback if labels < 30: #47 M + T-59 S = 5+2 = 7 h.
- Sprint 8 has **zero slack**. If T-48 or T-49 grows, T-52 moves to Sprint 9.
- Timing: the wide window starts Nov 2, so T-48 and the owner's repo variables must be done before Oct 26 (Sprint 8 exit); otherwise the 30-minute cadence stays inert.

**Definition of Done (all sprints):** `ruff check .`, `ruff format --check .`, `python -m pytest -x --tb=short -q`, mypy as configured in `quality.yml`; docs and a DECISIONS.md entry for any structural change. **Test plan:** fixtures only, no live network (`.claude/rules/testing.md`).

### Sprint 8: Pre-BF correctness and compliance

**Goal:** the cadence window, the zero-division guard, the legal citation and the 30-day rule wording are right before Black Friday.

| Item | Acceptance (Given / When / Then) |
|---|---|
| T-48 | Given the variables hold `2026-11-02T00:00:00+00:00` and `2026-12-01T23:59:00+00:00`, when the `*/30` cron fires on Nov 10, then `cadence_gate` prints `should_run=True`. Given a fire on Oct 30 or Dec 2, then `should_run=False`. Retro default window and QA window equal this window |
| T-49 | Given an alert with `old_price=0` or `thirty_day_low=0`, when analyze runs, then that alert is flagged and skipped, the others are formatted, and the exit code is 0 |
| T-50 | Given the owner's confirmed citation, when `grep "OUG 27/2022"` runs over `README.md`, `scripts/`, `tests/`, `docs/`, then no hit remains. `analyze.py:227` is unchanged unless the owner says otherwise |
| T-51 | Given a product with 20 days of history, when the reference price is computed, then it is the lowest price inside those 20 days (test). Given the same SKU at two retailers, then the windows never mix |
| T-52 | Given the pin, when `quality.yml` runs `uv sync --frozen`, then it passes |

**Risks:** T-49 changes batch behaviour in `analyze.py` close to the BF window (mitigation: fixture tests, golden-gate). T-52 version may not match `uv.lock`. **Exit:** owner variables set; CI green; T-50 blocked items listed if the owner has not answered.

### Sprint 9: BF freeze (docs and config only)

**Goal:** no runtime logic changes while the first peak (RO, Nov 6) runs.

| Item | Acceptance |
|---|---|
| T-56 | Given the owner's pasted robots.txt / ToS output, when the doc is written, then each retailer has a status line and a source command |
| T-54 | Given the owner's license choice, when merged, then LICENSE, CONTRIBUTING and SECURITY exist at repo root |
| T-60 | Given the runner risk, when the DECISIONS entry is read, then it lists the accepted risk, mitigations and re-open triggers |
| T-57 | Given the tracked JSON file, when the decision is recorded, then the chosen option and the migration trigger are written down |

**Risks:** owner inputs (license, curl output) late. **Exit:** no code change under `scripts/` or `bf_price_monitor/`.

### Sprint 10: Post-peak stabilisation

**Goal:** prepare the second peak (global, Nov 27) at lower cost, and tidy tooling and docs.

| Item | Acceptance |
|---|---|
| T-58 | Given the T-57 decision, when notify builds a chart, then it reads from SQLite and the alert output is unchanged on fixtures |
| T-61 | Given two windows, when now is inside either, then `should_run=True`; outside both, `False` |
| T-53 | Given the uv ecosystem entry, when the first Dependabot PR opens, then it changes `pyproject.toml` and `uv.lock`, else DECISIONS.md records the gap |
| T-55 | Given the refreshed docs, when read, then `docs/ROADMAP.md` carries the new sync date and `CLAUDE.md:19` no longer cites #56 as open |

**Risks:** T-58 touches the alert path right before Nov 27 (keep behind a fallback to the JSON file). **Exit:** CI green; runbook updated for two windows.

### Sprint 11: Retro and pilot

**Goal:** learn from the real event and decide on the anomaly pilot.

| Item | Acceptance |
|---|---|
| #47 | Given the event data, when `generate_retro_metrics.py` runs on the default window, then the retro doc is generated and reviewed |
| #36 | Given at least 30 labels, when the pilot is evaluated, then a go / no-go is recorded. Given fewer, then T-59 runs instead |

**Risks:** label count below 30 (fallback above). **Exit:** retro committed; #36 decision recorded.

## 7. Risk register (top 5)

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | BF repo variables not set before Oct 26, so the 30-minute cadence never starts | M | H | Owner action 2 below; runbook `docs/runbooks/BF_WEEK_CADENCE.md` |
| 2 | Retailer blocking or ToS exposure from the wide 30-day window (1,440 vs 360 attempts) | M | H | T-56 status, T-61 narrowing, watch the scrape health logs |
| 3 | Wrong legal citation stays in code and README | M | M | T-50 after owner check of primary texts |
| 4 | Public repo with a persistent self-hosted runner | L | H | Header warning in `monitor.yml:4-11`; T-60 records the accepted risk |
| 5 | Zero / None price aborts the whole analyze batch | L | H | T-49 guard and quarantine |

## 8. KPIs

| KPI | Target | Source |
|---|---|---|
| Sprint hours committed | at most 16 per sprint | section 6 |
| Quarantined alerts do not abort a batch | 0 batch aborts | T-49 test |
| Cadence window check | `should_run` true inside, false outside | T-48 test |
| Real feedback labels | at least 30 | `docs/feedback-labels.md` |
| CI | green on every merge | `quality.yml` |

## 9. Owner-only actions

None of these has been done by the author of this report.

1. Pick the license (T-54).
2. Set the BF repo variables before Oct 26:
   `gh variable set BF_PEAK_START_UTC --body "2026-11-02T00:00:00+00:00" --repo NaviAndrei/bf-price-monitor`
   `gh variable set BF_PEAK_END_UTC --body "2026-12-01T23:59:00+00:00" --repo NaviAndrei/bf-price-monitor`
   UI: Settings -> Secrets and variables -> Actions -> Variables -> New repository variable (twice). Check with `gh variable list --repo NaviAndrei/bf-price-monitor`. Format: ISO 8601 with offset (`docs/runbooks/BF_WEEK_CADENCE.md:20-21`). Roll back by deleting either variable (`:126-136`).
3. Open the primary texts on legislatie.just.ro (HG 686/2022, HG 947/2000, OUG 58/2022) and give the right citation per location (T-50).
4. Paste `curl -sS` output for `https://www.emag.ro/robots.txt`, `https://www.pcgarage.ro/robots.txt`, `https://www.flanco.ro/robots.txt` (Flanco may hit a challenge) plus each retailer's ToS URL (T-56). The ToS URLs: I don't know them.
5. Close #37 (acceptance met per `docs/progress.md`; GitHub still shows it open).
6. Tick or leave the boxes in `docs/BF_READINESS_SIGNOFF.md`; decide gate 4b.
7. Press feedback buttons on real alerts to reach at least 30 labels for #36.
8. Merge `master-fkamih` into master.

## 10. Sources

- Repo files at `origin/master` `1206be4`: `scripts/analyze.py`, `scripts/scrape.py`, `scripts/notify.py`, `scripts/cadence_gate.py`, `.github/workflows/monitor.yml`, `.github/workflows/quality.yml`, `.github/dependabot.yml`, `Dockerfile`, `README.md`, `CLAUDE.md`, `.claude/rules/*`, `docs/ROADMAP.md`, `docs/progress.md` (last 200 of 274 lines), `docs/DECISIONS.md` (headings and greps), `docs/BF_READINESS_SIGNOFF.md`, `docs/anomaly-pilot.md`, `docs/docker.md`, `docs/feedback-labels.md`, `docs/product-identity.md`, `docs/watchlist-qa.md`, `docs/runbooks/BF_WEEK_CADENCE.md`.
- GitHub issues #2-#69 via REST.
- UNVERIFIED: legal texts, retailer robots.txt / ToS, `RawAlertCandidate` constraints, `quality.yml:92`, retro script window line, `tests/` occurrences of "OUG 27/2022", current BF repo variable values.
