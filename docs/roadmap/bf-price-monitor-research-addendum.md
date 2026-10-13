# bf-price-monitor: External Research Addendum

Companion to [bf-price-monitor-roadmap.md](bf-price-monitor-roadmap.md) (v4, not edited). Derived report, not a source of truth: the roadmap lives in GitHub milestones and issues (see [CLAUDE.md](../../CLAUDE.md), "Roadmap source"). No issues were created; every ID below is a proposal. Date: 2026-10-13. Base: `master-fkamih` at `a1ea217`.

Marks: **UNVERIFIED** = not opened, or opened only in part, in this session. "I don't know" is stated where it applies. `[S#]` points to section 10; repo evidence is written as `path:line` and was read in this session.

## 1. Scope and method

| Item | Value |
|---|---|
| Logged search / fetch steps | 56 (section 11; 4 blocked, 3 discarded) |
| Topics | A architecture, B scraping reliability and ethics, C deal-quality logic, D alerting, E Black Friday, F papers, G similar projects, H engineering practice, I Claude Code setup, J overlap with repo issues |
| Method | Local files first (CLAUDE.md, `.claude/rules/*`, v4 roadmap, `docs/DECISIONS.md` headings). Then parallel batches, a synthesis pass, a gap-filling batch. Repo claims verified by reading the cited line or by running a read-only tool |
| Constraints kept | "Not building": Altex internal API, proxy rotation, forecasting, Pages dashboard, ephemeral runner, quiet hours / digest. Sprint capacity 20 h, 16 h committed, S=2 / M=5 / L=10, at most one L. Sprint 9 = BF freeze (docs and config only). Existing IDs T-48..T-61 kept |

Limits, stated plainly:

1. The `WebFetch` tool failed with a DNS error (`ENOTFOUND`) for `eur-lex.europa.eu`, `www.sqlite.org` and `www.rfc-editor.org`. The same public pages were then opened through the Exa fetch connector, a separate tool provided in this session. The proxy and network classifier were not bypassed. The text of Directive 2019/2161 itself was not opened; Article 6a is quoted from the Commission Notice [S1] and the CJEU judgment [S2].
2. `Scholar_Gateway` returned `ACCESS_DENIED`. Papers were found through SciSpace and Exa. A paper is marked "verified opened: yes" only if its abstract page or PDF text was opened here.
3. `search_repositories` on GitHub was called once for discovery (outside the repo scope note). Third-party READMEs were read through the web connector only. Issue lists of third-party projects were **not** inspected.
4. Measurements run here (read-only): `uv run --frozen mypy` per file, `pytest --cov=scripts` (694 passed, 1 skipped, 33.5 s). No `data/` file content and no `uv.lock` content was read.
5. Retailer 2026 Black Friday dates: I don't know (not announced in any opened source).

## 2. Key findings

1. **The legal citation in code is wrong.** OUG 27/2022 regulates electricity and gas price caps [S4]. The 30-day prior-price rule is Art. 4^1 of HG 947/2000, added by HG 686/2022, in force 28 May 2022, fines 5,000 to 30,000 lei [S3]. `README.md:52` and `scripts/scrape.py:1109` cite OUG 27/2022. Per its explanatory note, OUG 58/2022 amends Legea 363/2007, Legea 193/2000 and OUG 34/2014 and does not hold the price rule (full text read in part) [S5].
2. **"Same sales surface".** HG 686/2022 Art. 4^1(2) and the EU Notice both tie the prior price to the same sales surface / channel [S3][S1]. The repo scopes `thirty_day_low` per retailer product, not per channel (roadmap T-51 documents this).
3. **CJEU C-330/23 matches the rule engine.** A reduced price cannot equal or exceed the prior price [S2, para 27]. `scripts/analyze.py:129` (`new_price >= thirty_day_low` gives `FALSE_DISCOUNT`) already encodes this.
4. **Article 6a binds sellers, not observers.** It does not apply to price-comparison platforms [S1, 1.2]. Alerts from this repo are advisory signals, not legal findings; wording should say "suspected".
5. **Romanian BF starts earlier than the v4 window.** In 2025 eMAG ran on 7 November [S8]; Flanco 24 Oct to 30 Nov, evoMag 24 Oct to 9 Nov, Altex 30 Oct to 19 Nov, Fashion Days from 31 Oct [S9, secondary]. The v4 window starts 2026-11-02. 2026 dates: I don't know.
6. **Most "deals" are not best prices.** Which? found 83% to 98% of BF deals at eight UK retailers were the same or cheaper at other times, and for 6 in 10 deals the "was" price held for under half of the prior year [S10]. UK data, not Romanian.
7. **Self-hosted runner on a public repo is discouraged** by GitHub ("almost never") [S17][S18]. `.github/workflows/monitor.yml:13-23` triggers only on `schedule` and `workflow_dispatch`, which lowers the fork-PR risk; fork-PR approval settings are UNVERIFIED.
8. **Scheduled runs are delayed or dropped at the start of the hour** [S19]; routines docs give the same advice [S24]. `.github/workflows/monitor.yml:15` and `:22` fire at `:00` and every `:30`.
9. **A live-network test slipped through.** `tests/test_browser_state.py` reached `emag.ro` until commit `a1ea217`. `pytest-recording` ships `--block-network` [S30]; `tests/conftest.py:97` already holds an autouse guard for DB writes.
10. **Type and coverage gates stop at the package.** `.github/workflows/quality.yml:71` runs `mypy bf_price_monitor/` only (clean), although `pyproject.toml:50` sets `strict = true`. Measured mypy errors per script: `cadence_gate.py` 0, `analyze.py` 17, `notify.py` 47, `scrape.py` 64 (counts include followed imports). Coverage of `scripts/` is 85% (`cadence_gate.py` 63%) but `.github/workflows/quality.yml:73` gates only the package.
11. **JSON-LD parsing is permissive.** `scripts/scrape.py:604-627` accepts the first offer of any node with `@type == "Product"`, with no currency, positivity or list-type check, while Merchant Center requires `price`, `priceCurrency`, `availability` with `.` as decimal separator [S16].
12. **Dependency tooling is half-ready.** `pyproject.toml:5-14` uses `>=` floors, which suits Dependabot's `uv` ecosystem [S27]; `setup-uv` can read one pin from `pyproject.toml` (`required-version`) instead of three `version:` inputs [S25].

## 3. Industry best practices vs this repo

| Practice | Repo status | Evidence |
|---|---|---|
| Prior price = lowest price of the last 30 days, same sales surface | sufficient (logic), weak (scope, wording) | [S1][S3]; `scripts/analyze.py:126-138`; `scripts/scrape.py:1655-1661` |
| Hike-then-drop detection | sufficient | [S2]; `scripts/analyze.py:129` |
| "Was" price persistence (how long the reference was really charged) | missing | [S10]; `bf_price_monitor/storage/sqlite.py:41` stores `original_price`; no persistence metric |
| Correct legal citation | weak | [S3][S4]; `README.md:52`, `scripts/scrape.py:1109` |
| robots.txt compliance record | weak | [S11][S12]; adapters avoid disallowed paths (`scripts/scrape.py:937`, `:1014`) but no snapshot or test |
| Conditional requests (ETag / If-Modified-Since) | missing, deferred | [S13]; `scripts/scrape.py:875-911` sends none; whether retailers emit validators: UNVERIFIED |
| Honest crawler identification | weak, owner decision | [S11] (identification SHOULD describe purpose); `scripts/scrape.py:44`, `:879` use browser-like, rotated user agents |
| Structured data first, then semantic, then CSS | sufficient | `scripts/scrape.py:630-644`, `:562-578`; `.claude/rules/scraping.md` |
| Structured-data validation | weak | [S16]; `scripts/scrape.py:604-627` |
| Scraper health and drift monitoring | sufficient, one gap | issues #15, #67 (closed); [S49] suggests fallback-usage rate, which is not recorded (`scripts/scrape.py:664-673` returns a price only) |
| Headless-browser cost control | sufficient | [S33]; `scripts/scrape.py:173-182`, `:802` (`_block_heavy_requests`); one browser per run (DECISIONS T-21) |
| SQLite for single-writer local store | sufficient | [S14][S15]; `bf_price_monitor/storage/sqlite.py:245` (WAL), DB is runner-local |
| History compaction | not needed now | [S43] (drops datapoints equal to neighbours); DB size: I don't know |
| Alert dedup, cooldown, bounded retries, outbox | sufficient | `scripts/notify.py:60`, `:237-282`; issues #18, #20 |
| Human feedback loop | weak | 5 and 2 labels stored in two runs vs 30 needed (roadmap 4.2); issues #36, #37 |
| Test pyramid with recorded HTML fixtures and contract tests | sufficient | `tests/fixtures/`, `tests/test_contract_scrape.py`; issue #16 |
| No live network in tests, enforced | weak | [S30]; `.claude/rules/testing.md:10` is advisory; incident fixed in `a1ea217` |
| Mutation testing | missing | [S31] |
| Static typing | weak (package only) | `.github/workflows/quality.yml:71`; `pyproject.toml:50` |
| Coverage gate | weak (package only) | `.github/workflows/quality.yml:73`; scripts 85% measured |
| Scheduled-workflow reliability | weak | [S19]; `.github/workflows/monitor.yml:15`, `:22` |
| Self-hosted runner hygiene (public repo) | weak, accepted | [S17]; `.github/workflows/monitor.yml:13-23`; roadmap T-60 |
| Action pinning, least privilege | sufficient | `.claude/rules/security.md`; roadmap F5 |
| Dependency updates (uv) | weak | [S26][S27]; `.github/dependabot.yml:1-11` |
| SBOM | not needed | [S32]; no external consumers (CLAUDE.md) |
| DORA metrics | not measured | [S20]; proxies in section 8 |
| CLAUDE.md under 200 lines, path-scoped rules | sufficient | [S21]; `CLAUDE.md` 46 lines, rules 7 to 16 lines each |
| Hooks block with exit 2 | sufficient | [S22]; `.claude/settings.json` registers `branch_guard.py`; `.claude/hooks/` |
| Read-only reviewer subagents | sufficient | [S23]; `.claude/agents/verdict-integrity-reviewer.md` has `tools: Read, Grep, Glob` |
| Instruction drift | weak | `CLAUDE.md:23` says work on `master`; `branch_guard.py` now allows `master-fkamih`; `CLAUDE.md:12` says Windows only |

## 4. Papers

Only papers whose abstract or text was opened here are listed as findings. Authors "not shown" means the opened excerpt did not display them.

| # | Title | Authors | Year | Venue | Link | One-line finding | Applicability | Verified opened |
|---|---|---|---|---|---|---|---|---|
| P1 | Anomaly Detection for an E-commerce Pricing System | J. Ramakrishnan, E. Shaabani, C. Li, M. A. Sustik | 2019 | KDD 2019 (from DOI 10.1145/3292500.3330748 via SciSpace) | https://arxiv.org/abs/1902.09566 | Unsupervised and supervised anomaly detection in batch and streaming at Walmart; flagged items reviewed by priority and business impact [S34] | Med (T-49 quarantine, #36 pilot) | yes |
| P2 | Selective review of offline change point detection methods | C. Truong, L. Oudre, N. Vayatis | arXiv 2018 (id 1801); journal year UNVERIFIED | Signal Processing | https://arxiv.org/abs/1801.00718 | Detectors are a cost function, a search method and a constraint; ships the `ruptures` package [S36] | Med (`ruptures>=1.1.9` is in `pyproject.toml` extra `anomaly`) | yes |
| P3 | An Empirical Analysis of Algorithmic Pricing on Amazon Marketplace | L. Chen, A. Mislove, C. Wilson | 2016 | WWW 2016 | https://dl.acm.org/doi/10.1145/2872427.2883089 | 500+ algorithmic sellers found in 4 months over 1,641 products; for non-algorithmic sellers 65% of seller/product pairs never change price [S37] | Low-Med (explains price volatility, intraday cadence) | yes |
| P4 | Dynamic Pricing Recognition on E-Commerce Platforms with VAR Processes | A. Faehnle, M. Guidolin | 2021 | Forecasting 3(1), 166-180 | https://doi.org/10.3390/forecast3010011 | VAR with Lasso detects price moves driven by direct competitors on scraped smartphone prices [S38] | Low (competitor modelling; forecasting is "Not building") | yes |
| P5 | Judging LLM-as-a-judge with MT-Bench and Chatbot Arena | L. Zheng et al. | 2023 | NeurIPS Datasets and Benchmarks per my recollection, UNVERIFIED in excerpt | https://arxiv.org/abs/2306.05685 | GPT-4 judge agrees with humans over 80%; position, verbosity and self-enhancement biases; reference-guided judging cut failures from 70% to 15% on math items [S35] | Low (LLM only explains; verdict is deterministic) | yes |
| P6 | Entity Matching using Large Language Models | authors not shown | 2023 (arXiv 2310.11244) | not shown | https://arxiv.org/abs/2310.11244 | GPT-4 zero-shot matches or beats fine-tuned PLMs on 3 of 6 benchmarks and generalises to unseen entities [S39] | Low (identity adapters exist, issue #49) | yes |
| P7 | Using LLMs for the Extraction and Normalization of Product Attribute Values | authors not shown | 2024 (arXiv 2403.02130) | not shown | https://arxiv.org/abs/2403.02130 | GPT-4 reaches F1 91% on WDC-PAVE, 10% above PLM baselines [S40] | Low-Med (input to T-59) | yes |
| P8 | Evaluation of LLM-based Strategies for the Extraction of Food Product Information from Online Shops | authors not shown | 2025 (arXiv 2506.21585) | not shown | https://arxiv.org/abs/2506.21585 | Generated-extractor approach: 96.48% accuracy, 1.61 points below direct extraction, 95.82% fewer LLM calls on 3,000 pages [S41] | Med (T-59: generate, validate, cache) | yes |
| P9 | ExtractGPT: LLMs for product attribute value extraction | authors not shown | 2023 (arXiv 2310.12537) | not shown | https://arxiv.org/abs/2310.12537 | GPT-4 F1 85%; Llama-3-70B within 3 points [S42] | Low | yes |

Not found or not opened: any SIGIR or ACL paper on dynamic-pricing detection or short-horizon price forecasting (I don't know of one that was opened); the Billiger.de entity-matching hit was discarded because its arXiv id looked implausible.

## 5. Similar projects

Stars, license and dates come from search metadata or the opened README; blank means not shown. No code was copied; only ideas.

| Project | URL | Last activity | Stars | License | Three reusable ideas |
|---|---|---|---|---|---|
| Crinibus/scraper | https://github.com/Crinibus/scraper | push 2025-01-29 | 249 | MIT | (1) `request_delay` setting against bans; (2) "clean up data" removes datapoints equal to neighbours (compaction); (3) documented JSON-to-SQLite upgrade steps [S43] |
| PriceStalker | https://github.com/mikeknight85/PriceStalker | 2026-04 (day UNVERIFIED) | 36 | MIT | (1) per-retailer config with status history of failing retailers; (2) AI price verification catches "$189 off" read as "$189"; (3) consensus price selection across layers [S44] |
| andrewschultzw/price-tracker | https://github.com/andrewschultzw/price-tracker | not shown | not shown | not shown | (1) six cascading extraction strategies (JSON-LD, microdata, OpenGraph, CSS, regex, user selector); (2) per-seller cooldowns; (3) notification history with channel, time and savings [S45] |
| nuggetz/ha-amazon-price-tracker | https://github.com/nuggetz/ha-amazon-price-tracker | created 2026-05-04 | 6 | MIT | (1) randomised polling interval (about 4 h plus or minus 30 min); (2) stored historical minimum with its date; (3) on-demand `force_refresh` service [S46] |
| FaserF/ha-keyforsteam | https://github.com/FaserF/ha-keyforsteam | not shown | not shown | not shown | (1) JSON-LD as the stable source; (2) repair notice after 24 h of failures or a 404; (3) diagnostics output [S47] |

Leads not opened: `DAILtech/PriceDive` (search description mentions "hike-then-drop" detection; README not opened, UNVERIFIED).

## 6. New backlog items

IDs continue from T-61. Priority = Value / hours (S=2, M=5, L=10). Value 1 to 5. "Recurring mistake" is the failure the item stops from happening again.

### 6.1 New items

| ID | Title | Problem and recurring mistake prevented | Evidence | Repo touch point (verified) | V | Eff | Risk | Prio | Deps |
|---|---|---|---|---|---|---|---|---|---|
| T-62 | Network guard for tests (autouse fixture, no new dependency) | A test reached `emag.ro` on a real run. Mistake: a new test forgets to stub `fetch` | [S30]; commit `a1ea217`; `.claude/rules/testing.md:10` | `tests/conftest.py:97` (existing autouse guard to copy) | 5 | S | L | 2.5 | none |
| T-63 | JSON-LD offer validation | Wrong price enters history. Mistake: trusting `offers[0]` of any "Product" | [S16]; [S49] (weak, lead only) | `scripts/scrape.py:604-627` | 4 | M | M | 0.8 | none |
| T-64 | Reference-price persistence metric (shadow only) | Inflated "was" prices pass the 25% test. Mistake: judging the reference by one number, not by how long it held | [S10]; [S3] Art. 4^1(1) (proof of the prior price) | `scripts/scrape.py:1655-1661`, `scripts/analyze.py:126-138`, `bf_price_monitor/storage/sqlite.py:41` | 3 | M | M | 0.6 | T-58 not required; needs 30+ days of history |
| T-65 | Offline robots.txt conformance test | Adapters drift onto a disallowed path after an edit. Mistake: no record of what robots.txt said | [S11][S12] | `scripts/scrape.py:937`, `:1014` (comments only today); new snapshots under `docs/` | 3 | S | L | 1.5 | T-56 owner curl output |
| T-66 | Cron minute offsets and gate constant | Runs dropped or late at `:00` / `:30`. Mistake: scheduling on the busiest minute | [S19][S24] | `.github/workflows/monitor.yml:15`, `:22`; `scripts/cadence_gate.py:29`, `:50` | 3 | S | M | 1.5 | none (T-61 no longer a dependency) |
| T-67 | Extraction-layer hit-rate and JSON-LD vs visible price disagreement counters | A layer silently degrades while a fallback still fills the field | [S49]; Finding 11 | `scripts/scrape.py:664-673`, callers `:959`, `:1036`; health writer `:1354` | 3 | M | L | 0.6 | none |
| T-68 | Mutation-testing pilot on the rule engine (manual workflow, ubuntu) | Tests pass while a threshold or comparison is wrong. Mistake: 93% line coverage read as "tested" | [S31] (POSIX only) | `scripts/analyze.py:113` (`evaluate_omnibus_rule`), `:418` (`enforce_deterministic_invariant`) | 3 | M | L | 0.6 | none |
| T-69 | Widen mypy and coverage gates to `scripts/` in steps | Type and coverage regressions in the core scripts reach master unseen | measured: Finding 10 | `.github/workflows/quality.yml:71`, `:73`; `pyproject.toml:49-52` | 3 | M | L | 0.6 | none |
| T-70 | Alert evidence line (date and count behind the 30-day low) | User cannot judge a verdict. Mistake: showing a number without its basis | [S10]; [S3] Art. 4^1(4) (state the period) | `scripts/notify.py:105-114` (`_thirty_day_low_line`) | 3 | S | L | 1.5 | none |
| T-71 | Name and document the `1.25` inflated-reference threshold | Magic number with no rationale or audit trail | no external source for 1.25 (I don't know of one) | `scripts/analyze.py:133` | 2 | S | L | 1.0 | calibration waits for #36 labels |
| T-72 | Quarterly Claude Code setup audit | Instruction drift (branch policy, OS note, stale roadmap lines) | [S21] (`/doctor` trims, 200-line target) | `CLAUDE.md:12`, `:19`, `:23`; `docs/ROADMAP.md` | 2 | S | L | 1.0 | T-55 |

Notes:

- T-62 design: an autouse fixture that patches `socket.socket.connect` to raise for non-loopback addresses, with a regression test; `pytest-recording` is the reference [S30] but adds a dependency, so it is not proposed.
- T-63 scope: reject non-positive or non-finite price; accept only RON or absent currency; handle `@type` as a list; read `AggregateOffer.lowPrice` only when no `Offer` exists. Fixtures only.
- T-64 stays a shadow metric (logged, never changes the verdict) until #36 labels exist, to respect deterministic primacy.
- T-69 order: add `scripts/cadence_gate.py` (0 errors) and `scripts/analyze.py` (17 errors) to the mypy step; add `--cov=scripts` with a floor set from the then-current measurement (85% today). `notify.py` (47) and `scrape.py` (64) stay out until later.
- T-70 stays short: one line, same language as existing alerts.
- T-72 also covers the zero-hour scope note for T-55: `CLAUDE.md:23` vs the `branch_guard.py` allow-list.

### 6.2 Re-scored existing items (only where evidence contradicts v4)

| ID | v4 | New | Why |
|---|---|---|---|
| T-48 | Window 2026-11-02 to 2026-12-01 (30 d, up to 1,440 attempts); V5 / M / M | Same value and effort; recommended start about 2026-10-23, to be set after 2026 retailer dates appear. 40 d, up to 1,920 attempts vs 480 at the 2-hourly cadence | Early campaigns began 24 to 31 Oct in 2025 [S9]. The 2026 dates: I don't know. Owner variable deadline moves from Oct 26 to Oct 22 |
| T-50 | Dependency: owner confirms three acts; V4 / S / L / Prio 2.0 | No dependency; same V, effort and priority. Replace "OUG 27/2022" with "HG 947/2000 art. 4^1 (added by HG 686/2022)"; `scripts/analyze.py:227` may cite the same article | Primary texts opened [S3][S4][S5]. The acceptance grep must exclude `docs/roadmap/*`, which quotes the old string |
| T-61 | V2, Prio 1.0, Sprint 10 | V1, Prio 0.5, deferred until 2026 dates show a gap common to all watched retailers | Flanco ran 24 Oct to 30 Nov in 2025 [S9], so splitting the window would drop Flanco coverage in the middle. v4 assumed 7 to 25 Nov was off-peak |

T-60, T-52, T-53, T-59 keep their scores. Refinements only: T-60 cites [S17] and the verified trigger list; T-52 can use one `required-version` in `pyproject.toml` instead of three `version:` inputs [S25]; T-53 keeps `>=` floors [S27]; T-59 spec starts from the generate-validate-cache pattern [S41].

### 6.3 Rejected or deferred

| Idea | Source | Decision | Reason |
|---|---|---|---|
| Proxy or residential IP support | `hs-price-tracker`, `ha-amazon-price-tracker` roadmap, PriceStalker [S43-S47] | Rejected | "Not building: proxy rotation" |
| Price charts, sparklines, web dashboard | PriceStalker, Keepa, Crinibus [S43][S44][S48] | Rejected | "Not building: Pages dashboard"; static report exists (issue #34) |
| Short-horizon forecasting, VAR competitor model | P4 [S38] | Rejected | "Not building: forecasting" |
| Ephemeral or just-in-time runners | [S17] | Rejected | "Not building: ephemeral runner"; risk accepted in T-60 |
| Digest mode or quiet hours | Keepa notification settings [S48] | Rejected | "Not building: quiet hours / digest" |
| Conditional GET | [S13] | Deferred | No sign that retailers send validators; needs the owner probe in section 9 |
| Two-window cadence (T-61) | section 6.2 | Deferred | See re-score |
| LLM entity matching | P6 [S39] | Deferred | GTIN / SKU identity adapters exist (issue #49) |
| Honest bot user agent | [S11] | Deferred to owner | Changes how retailers see the scraper; ToS and block risk |

### 6.4 Considered, dropped

| Idea | Why dropped |
|---|---|
| Block images and fonts in Playwright | Already done: `scripts/scrape.py:173-182`, `:802` |
| Bounded retries, dead-letter queue, dedup | Already done: `scripts/notify.py:60`, `:237-282`; issues #18, #20 |
| Per-store health metrics | Already done: issues #15, #67 |
| JSON history to SQLite | Already done; remaining work is roadmap T-57 / T-58 |
| LLM-as-judge for verdicts | The deterministic invariant (`scripts/analyze.py:418-428`) already overrides the model; a judge adds cost and a second model to trust [S35] |
| SBOM generation | No external consumers (CLAUDE.md); `cyclonedx-py` does not read `uv.lock` directly [S32] |
| DORA dashboard | Solo project; section 8 uses cheap proxies instead [S20] |
| History compaction | WAL is fine at this size [S14]; re-open if the DB file passes a size the owner chooses (current size: I don't know) |
| `pytest-recording` / VCR | Adds a dependency; fixtures already cover retailer HTML [S30] |

## 7. Revised sprint plan 8 to 14

Capacity: 20 h per sprint, 20% buffer, 16 h committed. S=2, M=5, L=10, at most one L per sprint. Dates follow the v4 grid; Sprints 12 to 14 are post-BF (Dec 7 onward).

| Sprint | Dates | Items | Arithmetic | Hours | L |
|---|---|---|---|---|---|
| 8 | Oct 12-25 | T-48 M, T-49 M, T-50 S, T-51 S, T-62 S | 5+5+2+2+2 | 16 | 0 |
| 9 (BF freeze) | Oct 26-Nov 8 | T-52 S, T-54 S, T-55 S, T-56 S, T-57 S, T-60 S | 2+2+2+2+2+2 | 12 | 0 |
| 10 | Nov 9-22 | T-63 M, T-66 S, T-53 S, T-65 S | 5+2+2+2 | 11 | 0 |
| 11 | Nov 23-Dec 6 | #47 M, #36 L (only with 30+ labels) | 5+10 | 15 | 1 |
| 12 | Dec 7-20 | T-58 M, T-64 M, T-70 S, T-71 S | 5+5+2+2 | 14 | 0 |
| 13 | Dec 21-Jan 3 | T-67 M, T-68 M, T-69 M | 5+5+5 | 15 | 0 |
| 14 | Jan 4-17 | Path A: T-59 S, T-72 S. Path B: T-72 S, #36 L | A: 2+2; B: 2+10 | A: 4; B: 12 | A: 0; B: 1 |

- Path A (30+ labels on Nov 23): Sprint 11 = 15 h, Sprint 14 = 4 h. Total 16+12+11+15+14+15+4 = 87 h.
- Path B (fewer labels): Sprint 11 falls back to #47 M + T-59 S = 5+2 = 7 h; #36 moves to Sprint 14 with T-72 S = 10+2 = 12 h. Total 16+12+11+7+14+15+12 = 87 h.
- What moved from v4: T-52 to Sprint 9 (config only; v4 already named this as the relief valve), T-55 to Sprint 9 (docs), T-58 to Sprint 12 (keeps the alert path untouched before the Nov 27 peak), T-61 deferred, T-59 to Sprint 14 on path A.
- Sprint 8 stays at zero slack. If T-48 or T-49 grows, T-62 is the last item to cut, because it is the cheapest guard against live calls while scraper code changes.
- Sprint 9 contains no change under `scripts/`, `bf_price_monitor/` or tests. T-65 waits for Sprint 10 for that reason.
- Sprint 13 spans the year-end holidays: treat its 15 h as at risk.

**Definition of Done (all sprints):** `ruff check .`; `ruff format --check .`; `python -m pytest -x --tb=short -q`; `mypy` as in `quality.yml` (`uv run mypy bf_price_monitor/`, plus `scripts/cadence_gate.py` and `scripts/analyze.py` after T-69); a `docs/DECISIONS.md` entry for any structural change. **Test plan:** fixtures only, no live network (`.claude/rules/testing.md`).

### Sprint 8: Pre-BF correctness and compliance

Goal: the cadence window, the zero-price guard, the legal citation, the 30-day wording and a network guard are in place before the first retailer campaigns.

| Item | Given / When / Then |
|---|---|
| T-48 | Given the variables hold the owner-chosen start and `2026-12-01T23:59:00+00:00`, when the `*/30` cron fires inside the window, then `cadence_gate` prints `should_run=True`; outside it, `False`. Retro default window and QA window equal the chosen window. `cadence_gate.py` coverage rises from 63% (its uncovered branches get tests) |
| T-49 | As in the v4 roadmap |
| T-50 | Given the correct citation, when `grep "OUG 27/2022"` runs over `README.md`, `scripts/`, `tests/`, `docs/` except `docs/roadmap/`, then no hit remains |
| T-51 | As in the v4 roadmap |
| T-62 | Given a test that calls a non-loopback `socket.connect`, when pytest runs, then it fails with a message naming the host. Given loopback or a stubbed `fetch`, then it passes. Test plan: a unit test for the guard itself; no real connection |

Risks: Sprint 8 is full; the owner variables must exist by Oct 22 if the start is Oct 23. Exit: variables set (owner), CI green, T-50 done, guard merged.

### Sprint 9: BF freeze (docs and config only)

Goal: no runtime change while the first peak (Nov 6) runs.

| Item | Given / When / Then |
|---|---|
| T-52 | Given a single `required-version` in `pyproject.toml` (or the `version:` pin), when `quality.yml` and `monitor.yml` run `uv sync --frozen`, then both pass |
| T-54, T-55, T-56, T-57, T-60 | As in the v4 roadmap. T-55 also fixes the `CLAUDE.md:23` branch note. T-60 cites [S17] and lists the verified triggers |

Risks: owner input (license, curl output) late; a `monitor.yml` edit during BF needs a dry run first. Exit: no diff under `scripts/`, `bf_price_monitor/`, `tests/`.

### Sprint 10: Post-peak hardening

Goal: validate prices at the source and prepare the Nov 27 peak with fewer dropped runs.

| Item | Given / When / Then |
|---|---|
| T-63 | Given JSON-LD fixtures with price 0, `NaN`, currency EUR, `@type` as a list and an `AggregateOffer`, when extraction runs, then zero, `NaN` and EUR are rejected, the list type and aggregate are accepted, and the CSS layer still runs after a rejection |
| T-66 | Given the new cron strings, when `cadence_gate.should_run` receives the tight schedule string, then it matches the constant; the old string no longer matches. `BF_WEEK_CADENCE.md` is updated |
| T-53 | As in the v4 roadmap |
| T-65 | Given the owner's robots.txt snapshots, when the test builds each adapter's request paths, then none match a `Disallow` rule for the adapter's user agent |

Risks: T-63 touches extraction ten days before Nov 27 (mitigation: fixtures, golden gate, fallback to the CSS layer). T-66 changes schedule strings: ship constant and workflow together. Exit: CI green; one real scheduled run observed after the cron change.

### Sprint 11: Retro and pilot

Unchanged from v4: #47 M, then #36 L only with 30+ labels. Fallback: #47 + T-59 (7 h). Exit: retro committed; #36 go / no-go recorded. P1 and P2 [S34][S36] are the reading list for #36.

### Sprint 12: Post-BF quality

Goal: move the chart source off the JSON file and add evidence to the verdict.

| Item | Given / When / Then |
|---|---|
| T-58 | As in the v4 roadmap |
| T-64 | Given a product history with a reference price present on 20% of days, when analyze runs, then the shadow field `reference_persistence_pct` is logged as 20 and the verdict is unchanged |
| T-70 | Given an alert, when it is formatted, then the 30-day-low line shows the date of that low and the observation count, in one line |
| T-71 | Given the threshold, when the audit record is written, then it names the constant and its value; behaviour is unchanged on fixtures |

Risks: T-64 needs enough history (UNVERIFIED how many products have 30+ days). Exit: golden gate green, DECISIONS entries for T-64 and T-58.

### Sprint 13: Test and type depth

Goal: find weak tests and widen the gates. Holiday sprint.

| Item | Given / When / Then |
|---|---|
| T-67 | Given fixture pages, when a run ends, then health records contain the layer that produced each price, the fallback-usage rate, and a count of JSON-LD vs visible price disagreements |
| T-68 | Given a manual `workflow_dispatch` on `ubuntu-latest`, when mutmut runs on the two functions, then a report lists surviving mutants and no repo file changes |
| T-69 | Given the new CI step, when `mypy` runs on `cadence_gate.py` and `analyze.py`, then it exits 0; `--cov=scripts` enforces a floor equal to the measured value minus 2 points |

Risks: T-69 (17 errors in `analyze.py`) may spill; cut `analyze.py` first and keep `cadence_gate.py`. Exit: CI green; mutation report attached to the issue.

### Sprint 14: Buffer and audit

Goal: close carry-overs and re-sync instructions.

| Item | Given / When / Then |
|---|---|
| T-59 | Given the generate-validate-cache pattern, when the spec is read, then it states that the AI tier never sets a price without a rule-based cross-check |
| T-72 | Given a `/doctor`-style review, when finished, then `CLAUDE.md`, rules and `docs/ROADMAP.md` carry no stale line (branch policy, OS note, closed issue references) |
| #36 | Only if Sprint 11 fell back: as in the v4 roadmap |

Risks: labels still below 30. Exit: roadmap re-planned for Q1.

## 8. Risk register and KPIs

### Risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| 1 | BF variables unset or start date late (retailer campaigns begin about Oct 23 to 30) | M | H | Owner action 1; T-48 |
| 2 | Retailer blocking or ToS exposure from a 40-day window (up to 1,920 attempts vs 480) | M | H | T-56, owner probe, health logs, defer T-61 only if dates allow |
| 3 | Wrong legal citation stays public | M | M | T-50 (primary texts now opened) |
| 4 | A test calls a live site | M | M | T-62 |
| 5 | Dropped or late scheduled runs at `:00` / `:30` | M | M | T-66; observe real firing rate (I don't know it) |
| 6 | Structured data and visible price disagree, so a wrong price becomes history | M | H | T-63, T-67 (rate UNVERIFIED) |
| 7 | Forked-PR code reaching the self-hosted runner if repo settings are permissive | L | H | Verified triggers; owner action 3 for the settings (UNVERIFIED) |
| 8 | Labels stay under 30, blocking #36 | M | M | Sprint 11 fallback, Sprint 14 slot |
| 9 | Year-end holiday cuts Sprint 13 capacity | M | L | T-69 splits by file |

### KPIs

Targets marked "proposal" are suggestions, not agreed values. Baselines marked "I don't know" were not measured here.

| KPI | Baseline | Target | How measured |
|---|---|---|---|
| Alert precision = useful / (useful + wrong + fake) | I don't know (5 and 2 stored in two runs) | proposal: at least 0.8 at 30+ labels | feedback label report (`docs/feedback-labels.md`) |
| Scrape success rate per retailer per run | I don't know | proposal: at least 95% | `data/scrape_health.jsonl` (not read here) |
| p95 workflow run time | I don't know (one run: 40 s vs 42 s, issue #29) | proposal: no more than baseline + 20% | Actions run durations |
| Extraction-layer hit-rate (JSON-LD / semantic / CSS share) | not recorded | recorded after T-67 | health records |
| JSON-LD vs visible price disagreement | not recorded | proposal: under 1% | T-67 counter |
| Quarantined alerts per run | not recorded | 0 batch aborts | T-49 test and logs |
| mypy errors in `scripts/` | analyze 17, cadence_gate 0, notify 47, scrape 64 (measured 2026-10-13) | first two at 0 after Sprint 13 | `uv run mypy <file>` |
| Coverage of `scripts/` | 85%, `cadence_gate.py` 63% (measured) | not lower; `cadence_gate.py` above 85% after T-48 | `pytest --cov=scripts` |
| DORA proxy: deploys per week | I don't know | track only | pushes to master |
| DORA proxy: change fail rate | I don't know | track only | share of master pushes followed by a red `quality.yml` within 24 h [S20] |

## 9. Owner-only actions (new ones only)

None of these has been done by the author of this report. Earlier actions stay in section 9 of the v4 roadmap, except item 2 there, which is replaced by action 1 below.

1. **Choose the BF window after 2026 retailer dates appear, and set the variables by Oct 22 if the start is Oct 23.** Example (adjust the start to the chosen date):
   `gh variable set BF_PEAK_START_UTC --body "2026-10-23T00:00:00+00:00" --repo NaviAndrei/bf-price-monitor`
   `gh variable set BF_PEAK_END_UTC --body "2026-12-01T23:59:00+00:00" --repo NaviAndrei/bf-price-monitor`
   Check with `gh variable list --repo NaviAndrei/bf-price-monitor`. Roll back by deleting either variable ([BF_WEEK_CADENCE.md](../runbooks/BF_WEEK_CADENCE.md)).
2. **Probe validators for conditional requests.** For one listing URL per retailer taken from the watchlist: `curl -sSI "<listing-url>"`, and paste the `ETag`, `Last-Modified` and `Cache-Control` lines. Listing URLs: I don't know them.
3. **Check repo settings:** Settings -> Actions -> General, the fork pull request workflow approval option, and whether any runner group exposes the self-hosted runner to other repos. Exact option names: UNVERIFIED.
4. **Decide the user-agent policy:** keep browser-like rotated agents (`scripts/scrape.py:44`, `:879`) or identify as a bot [S11]. Either choice goes into `docs/DECISIONS.md`.
5. **Optional:** read the consolidated HG 947/2000 on legislatie.just.ro to confirm the wording T-50 will use.
6. **If these items are accepted, create the issues** (T-62 to T-72). No issue was created here.

## 10. Source list

Accessed 2026-10-13. Status: opened = page text read here (excerpt noted where partial); not opened = listed in "could not open".

| ID | Source | Status |
|---|---|---|
| S1 | Commission Notice 2021/C 526/02 on Art. 6a, https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52021XC1229(06) | opened |
| S2 | CJEU C-330/23, Aldi Süd, 26 Sep 2024, https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A62023CJ0330 | opened |
| S3 | HG 686/2022, https://legislatie.just.ro/Public/DetaliiDocument/255732 | opened |
| S4 | OUG 27/2022, https://legislatie.just.ro/Public/DetaliiDocument/252777 | opened (excerpt) |
| S5 | OUG 58/2022, https://legislatie.just.ro/Public/DetaliiDocument/254946 and https://gov.ro/fisiere/subpagini_fisiere/NF_OUG_58-2022.pdf | opened (excerpts) |
| S6 | Juridice.ro on HG 686/2022 (secondary), https://www.juridice.ro/687203/update-legislativ-campaniile-de-reduceri-de-pret-in-lumina-directivei-omnibus.html | opened |
| S7 | ANPC, controls on reductions, https://anpc.ro/anpc-continua-sa-descopere-probleme-la-campaniile-de-reduceri-in-toata-tara/ (undated in excerpt: 22 fines, 359,000 lei) | opened (excerpt) |
| S8 | eMAG press release 2025-11-03, https://about.emag.ro/2025/11/03/ | opened (excerpt) |
| S9 | Romanian news on BF 2025 dates (secondary): startupcafe.ro 2025-10-23, primapagina.ro 2025-10-31, livetext.ro 2025-10-27, mobilissimo.ro live blog | opened (excerpts) |
| S10 | Which? Black Friday studies 2023, 2024, 2025, https://www.which.co.uk/policy-and-insight/article/dont-believe-the-hype-most-black-friday-deals-the-same-price-or-cheaper-at-other-times-of-the-year-which-finds-a3TbD5x5KADs | opened |
| S11 | RFC 9309, https://www.rfc-editor.org/rfc/rfc9309 | opened (partial) |
| S12 | Google robots.txt specification, https://developers.google.com/crawling/docs/robots-txt/robots-txt-spec | opened |
| S13 | MDN conditional requests and If-Modified-Since; RFC 9110 | MDN opened; RFC 9110 excerpt only |
| S14 | SQLite WAL, https://www.sqlite.org/wal.html | opened |
| S15 | SQLite "Appropriate uses", https://www.sqlite.org/whentouse.html | opened (partial) |
| S16 | Google Merchant Center structured data help, https://support.google.com/merchants/answer/7331077 and /6386198 | opened |
| S17 | GitHub Docs, Secure use reference (self-hosted runners), https://docs.github.com/en/actions/reference/security/secure-use | opened |
| S18 | GitHub Docs, Adding self-hosted runners | opened (excerpt) |
| S19 | GitHub Docs, Events that trigger workflows (schedule) and Troubleshooting workflows | opened |
| S20 | DORA, https://dora.dev/guides/dora-metrics/ | opened |
| S21 | Claude Code docs, Memory, https://code.claude.com/docs/en/memory | opened |
| S22 | Claude Code docs, Hooks, https://code.claude.com/docs/en/hooks.md | opened |
| S23 | Claude Code docs, Subagents, https://code.claude.com/docs/en/sub-agents | opened |
| S24 | Claude Code docs, Scheduled tasks and Routines | opened |
| S25 | astral-sh/setup-uv README, https://github.com/astral-sh/setup-uv | opened |
| S26 | Dependabot supported ecosystems, GitHub Docs | opened; the `uv` row was not seen (UNVERIFIED) |
| S27 | dependabot-core issue 12273, https://github.com/dependabot/dependabot-core/issues/12273 | opened |
| S28 | DataBoar ADR-0044 (a third-party repo's decision record on Dependabot `uv`) | opened (weak) |
| S29 | pydevtools handbook on maintaining a uv project (claims a 3-day Dependabot cooldown; UNVERIFIED) | opened (weak) |
| S30 | pytest-recording, https://pypi.org/project/pytest-recording/ | opened |
| S31 | mutmut, https://mutmut.readthedocs.io/en/latest/ | opened |
| S32 | cyclonedx-python docs; Taketo-Yoda/uv-sbom | opened (weak for uv) |
| S33 | Playwright Python, Network, https://playwright.dev/python/docs/network | opened |
| S34 | P1, arXiv 1902.09566 | opened |
| S35 | P5, arXiv 2306.05685 | opened |
| S36 | P2, arXiv 1801.00718 | opened |
| S37 | P3, https://dl.acm.org/doi/10.1145/2872427.2883089 and https://mislove.org/publications/Amazon-WWW.pdf | opened |
| S38 | P4, https://www.mdpi.com/2571-9394/3/1/11 | opened |
| S39 | P6, arXiv 2310.11244 | opened |
| S40 | P7, arXiv 2403.02130 | opened |
| S41 | P8, arXiv 2506.21585 | opened |
| S42 | P9, arXiv 2310.12537 | opened |
| S43 | Crinibus/scraper README and GitHub search metadata | opened |
| S44 | mikeknight85/PriceStalker README (via search) | opened (excerpt) |
| S45 | andrewschultzw/price-tracker README (via search) | opened (excerpt) |
| S46 | nuggetz/ha-amazon-price-tracker README (via search) | opened (excerpt) |
| S47 | hwajin-me/hs-price-tracker and FaserF/ha-keyforsteam READMEs (via search) | opened (excerpts) |
| S48 | Keepa Chrome Web Store listing; ecommerce-platforms.com and revenuegeeks.com comparisons | opened (comparisons are weak) |
| S49 | Scraper-drift practitioner posts: apiserpent.com, scrapes.us, hilgard.cz | opened (weak; leads only) |
| S50 | GitHub issue search in NaviAndrei/bf-price-monitor (overlap check, topic J) | opened |
| S51 | GitHub repository search metadata (stars, license, push date) | opened |

Could not open or not used:

- `eur-lex.europa.eu` CELEX:32019L2161 (Directive text), `www.sqlite.org`, `www.rfc-editor.org` via `WebFetch`: DNS error. Read through the Exa connector instead, except the Directive text.
- Scholar Gateway search: access denied.
- Telegram Bot API rate-limit numbers: not seen in the opened excerpt.
- Dependabot `uv` row in the official table; Google Search Central Product docs (only Merchant Center help opened); full RFC 9110.
- Third-party issue lists; `DAILtech/PriceDive` README.

## 11. Step log

Format: step N: query -> source -> keep / discard.

- step 1: WebSearch: Directive 2019/2161 Art 6a lowest price 30 days -> results list; led to Commission Notice 2021/C 526/02 -> keep (lead)
- step 2: WebFetch eur-lex CELEX:32019L2161 -> DNS ENOTFOUND (WebFetch tool cannot resolve hosts) -> blocked
- step 3: WebFetch sqlite.org/whentouse -> DNS ENOTFOUND -> blocked
- step 4: WebFetch rfc-editor RFC 9309 -> DNS ENOTFOUND -> blocked
- step 5: Exa fetch EUR-Lex Commission Notice 2021/C 526/02 (partial, intro+TOC) -> opened -> keep
- step 6: Exa fetch RFC 9309 -> opened (partial) -> keep
- step 7: Exa fetch sqlite.org/whentouse -> opened (partial) -> keep
- step 8: Exa search: Romanian transposition Omnibus -> legislatie.just.ro OUG 140/2021 (goods sale, not price), ANPC draft HG 947 amendment, juridice.ro -> keep
- step 9: Scholar_Gateway: fake discounts price history -> ACCESS_DENIED -> blocked
- step 10: SciSpace: price anomaly / dynamic pricing detection -> 10 hits (KDD 2019 anomaly detection e-commerce pricing; Faehnle VAR dynamic pricing recognition) -> keep (leads)
- step 11: Exa fetch juridice.ro 687203 (HG 686/2022 -> HG 947/2000) -> opened; secondary source (law firm article) -> keep (weak; not primary)
- step 12: Exa fetch EUR-Lex Notice 2021/C 526/02 full text (30 kB) -> opened: prior price = lowest in >=30 d incl. earlier reduced prices; per-sales-channel; 'Black Friday offers' are covered; Art 6a applies to the seller, not to aggregators/comparison sites -> keep (primary)
- step 13: Exa search: Home Assistant / self-hosted price trackers -> ha-amazon-price-tracker (MIT, 6 stars, created 2026-05), hs-price-tracker, PriceStalker (MIT, 36 stars), andrewschultzw/price-tracker (SQLite, cooldowns) -> keep (leads; star counts from search metadata)
- step 14: GitHub search_repositories 'price tracker scraper self-hosted language:Python stars:>200' -> 0 results (query syntax) -> discard
- step 15: GitHub search_repositories 'price tracker python scraper' sort=stars -> saved to file; parsed below -> keep
- step 16: Exa search: MDN/RFC 9110 conditional requests -> MDN Conditional_requests, If-Modified-Since, RFC 9110 opened via highlights -> keep (primary)
- step 17: Exa search: Claude Code memory/CLAUDE.md docs -> code.claude.com/docs/en/memory: target <200 lines per CLAUDE.md; paths frontmatter; /doctor trims -> keep (primary)
- step 18: Exa search: GitHub secure use / self-hosted runners -> docs.github.com secure-use: self-hosted runners should almost never be used for public repositories; JIT runners; adding-self-hosted-runners warning -> keep (primary)
- step 19: Exa fetch arxiv.org/abs/1907.04011 (guessed id) -> WRONG paper (UnsuperPoint, cs.CV) - id was my guess -> discard (never cited)
- step 20: Exa fetch mdpi.com/2571-9394/3/1/11 (Faehnle, Dynamic Pricing Recognition VAR) -> page opened but abstract text not rendered (site chrome only) -> keep as lead; verified opened = NO (abstract unseen)
- step 21: Exa fetch Dependabot supported-ecosystems page -> opened, output truncated before the uv row -> keep (re-check uv row)
- step 22: Exa search: Walmart anomaly detection paper -> arXiv 1902.09566 abstract opened (Ramakrishnan, Shaabani, Li, Sustik; 2019; KDD 2019 per DOI 10.1145/3292500.3330748 from SciSpace) -> keep (verified yes)
- step 23: Exa search: LLM web data extraction product pages -> arXiv 2506.21585, 2403.02130, 2306.14921, 2310.12537 abstracts opened -> keep
- step 24: Exa search: LLM entity matching -> arXiv 2310.11244 (Entity Matching using LLMs), 2409.08185 (fine-tuning LLMs for EM) opened; a 'Billiger.de Products' hit has a suspicious arXiv id 2609.37713 -> keep first two; discard Billiger (id not trusted)
- step 25: Exa search: Dependabot uv ecosystem -> pydevtools handbook (weak), DataBoar ADR-0044 (a third-party repo's ADR: uv ecosystem moves pyproject+uv.lock together), dependabot-core issue #12273 (unconstrained deps not bumped; reported fixed by #13160 in a later comment) -> keep with caveats; official docs row for uv not seen
- step 26: Exa search: Telegram Bot API limits -> core.telegram.org/Bots/api opened; rate-limit numbers NOT seen in excerpt -> discard for numbers (no claim)
- step 27: Exa search: Romanian BF 2025 timeline -> livetext.ro, startupcafe.ro, primapagina.ro, about.emag.ro press release (primary for eMAG: BF Nov 7 2025, orders delivered by Nov 22), mobilissimo live blog -> keep: eMAG Nov 7 2025 (Nov 8 2024, Nov 10 2023); Flanco Oct 24-Nov 30; Altex Oct 30-Nov 19; evoMag Oct 24-Nov 9; PC Garage 'Black Friday Devreme' (dates unseen). News sites are secondary
- step 28: Exa search: DORA metrics -> dora.dev/guides/dora-metrics: five metrics (change lead time, deployment frequency, failed deployment recovery time, change fail rate, deployment rework rate) -> keep (primary)
- step 29: GitHub issue search: robots/ETag/conditional/If-Modified-Since in repo -> 1 hit: #60 (pwsh runner cleanup) - no overlap with conditional requests or robots -> keep (J: no duplicate for T-56-style/ETag ideas beyond roadmap)
- step 30: GitHub issue search: mutation/SBOM/dependabot/fixtures/health/drift in repo -> large result saved; parsed below -> keep (J)
- step 31: Exa search: Crinibus/scraper README -> opened README: records.json -> SQLite migration, request_delay setting, 'clean up data' removes datapoints equal to neighbours (compaction idea), search/visualize CLI; 249 stars MIT last push 2025-01-29 (GitHub search metadata) -> keep
- step 32: Exa search: LLM-as-a-judge paper -> arXiv 2306.05685 (Zheng et al., Berkeley/UCSD/CMU/Stanford; NeurIPS 2023 Datasets & Benchmarks per my recollection, venue line not in opened excerpt) abstract opened: >80% GPT-4/human agreement; position/verbosity/self-enhancement bias; reference-guided judging cut failure 70%->15% on math/reasoning -> keep (verified yes; venue UNVERIFIED in excerpt)
- step 33: Exa search: Keepa/CamelCamelCamel features -> Chrome Web Store Keepa listing (primary), ecommerce-platforms.com + revenuegeeks.com (secondary): lowest/highest/average per price type; out-of-stock gaps; target-price alerts -> keep (weak for specifics)
- step 34: Exa search: mutmut docs -> PyPI mutmut 3.8.0 + readthedocs opened: POSIX fork only (Windows needs WSL); incremental re-runs; max_stack_depth -> keep (primary)
- step 35: Exa search: Claude Code hooks docs -> code.claude.com/docs/en/hooks.md opened: exit 2 blocks (PreToolUse, Stop, ...), exit 1 does not block; project hooks in .claude/settings.json; InstructionsLoaded event -> keep (primary)
- step 36: Exa search: SQLite WAL -> sqlite.org/wal.html + wal_checkpoint_v2 opened: readers don't block writer, one writer, not on network filesystems, checkpoint starvation -> keep (primary)
- step 37: Exa search: Google Merchant structured data -> support.google.com/merchants/answer/7331077 and 6386198 opened: Offer price with '.' decimal, priceCurrency ISO 4217, availability; sale_price needs StrikethroughPrice priceSpecification -> keep (primary)
- step 38: Exa search: GitHub Actions schedule event docs -> docs.github.com events-that-trigger-workflows + troubleshoot-workflows opened: schedule can be delayed/dropped under high load, esp. at start of every hour; min interval 5 min; public repos: scheduled workflows auto-disabled after 60 days without repo activity -> keep (primary)
- step 39: Exa search: CJEU C-330/23 Aldi Sud -> EUR-Lex 62023CJ0330 opened: percentage reduction must be determined from the prior price; the 'reduced' selling price cannot equal or exceed the prior price (para 27) -> keep (primary)
- step 40: Exa search: pytest-recording / vcrpy -> PyPI + GitHub README opened: default record mode 'none'; --block-network; allowed_hosts -> keep (primary)
- step 41: Exa search: SBOM for uv projects -> cyclonedx-python docs: uv lockfile NOT explicitly supported (use venv); Taketo-Yoda/uv-sbom reads uv.lock (third-party tool, not vetted) -> keep with caveat
- step 42: Exa search: ANPC BF controls -> anpc.ro press page (undated in excerpt) opened: 22 fines totalling 359,000 lei, typical finding 'reference price not the lowest of last 30 days'; wall-street.ro/juridice.ro/playtech.ro (secondary) mention OG 99/2000, patterns: wrong reference price, stock, price mismatch cart -> keep (anpc.ro primary; others weak)
- step 43: Exa search: ruptures survey -> arXiv 1801.00718 Truong, Oudre, Vayatis; Signal Processing; abstract opened; ruptures lib is already an optional extra in pyproject.toml -> keep (verified yes)
- step 44: Exa search: Chen, Mislove, Wilson WWW 2016 -> mislove.org PDF + ACM page opened: methodology to detect algorithmic pricing on Amazon; 4 months, 1,641 best-seller products, 500+ algorithmic sellers; non-algorithmic: 65% of seller/product pairs never change price -> keep (verified yes)
- step 45: Exa search: Faehnle & Guidolin VAR dynamic pricing -> MDPI Forecasting 2021, 3(1), 166-180, doi 10.3390/forecast3010011; abstract opened this time: VAR+Lasso on Italian smartphone prices, 2 months scraped -> keep (verified yes; supersedes step 20 'no')
- step 46: Exa search: Playwright network routing -> playwright.dev/python/docs/network + class-route opened: page.route + route.abort by resource_type to skip images -> keep (primary)
- step 47: Exa search: Google robots.txt spec -> developers.google.com robots-txt-spec opened: 4xx (except 429) = no robots file; 5xx = stop for 12 h then use last good copy up to 30 days; cache up to 24 h; second independent source for RFC 9309 behaviour -> keep (primary)
- step 48: Exa search: Which? Black Friday studies -> which.co.uk 2023/2024/2025 pages opened: 83-98% of BF deals same/cheaper at other times (UK, 8 retailers, 175-227 products); 2024: for 6 in 10 deals the 'was' price held <50% of prior year, 14 deals never charged; RRP vs was-price distinction -> keep (primary for UK; not Romanian)
- step 49: Exa search: astral-sh/setup-uv README -> opened: without `version` the action reads required-version from uv.toml/pyproject.toml else installs latest; enable-cache auto disabled on self-hosted; SHA-pinned examples -> keep (primary)
- step 50: Exa search: Claude Code subagents docs -> code.claude.com/docs/en/sub-agents opened: .claude/agents/*.md, frontmatter name+description required, tools allow-list, model -> keep (primary)
- step 51: Exa search: scraper drift monitoring practice -> apiserpent.com, scrapes.us, hilgard.cz, 0xConsole/scraper-health-mcp (vendor/practitioner blogs; weak): result-count assertions, null-rate, canary pages, fallback-usage rate; JSON-LD price may differ from visible discounted price (single practitioner claim) -> keep as leads only (weak)
- step 52: Exa search: HG 686/2022 on legislatie.just.ro -> PRIMARY opened (legislatie.just.ro/Public/DetaliiDocument/255732, MO 520/26.05.2022): adds Art. 4^1 to HG 947/2000: prior price = lowest price in the SAME SALES SURFACE in the last 30 days; fines 5,000-30,000 lei (Art. 12(2)); in force 28 May 2022; transposes Art. 2 of Directive 2019/2161 -> keep (primary) - resolves T-50 citation
- step 53: Exa search: OUG 27/2022 -> PRIMARY opened (legislatie.just.ro/Public/DetaliiDocument/252777): OUG 27/2022 = energy price caps for electricity and gas (1 Apr 2022-31 Mar 2023, later extended); NOT about price-reduction announcements -> keep (primary) - README.md:52 and scrape.py:1109 citation is wrong
- step 54: Exa search: Claude Code scheduled tasks / routines docs -> code.claude.com/docs/en/scheduled-tasks + routines opened: session-scoped /loop tasks expire after 7 days; routines min interval 1 hour; 'pick a few minutes past the hour' to avoid late starts -> keep (primary)
- step 55: Exa search: OUG 58/2022 -> PRIMARY opened (legislatie.just.ro/Public/DetaliiDocument/254946 + gov.ro note de fundamentare): OUG 58/2022 amends Legea 363/2007, Legea 193/2000 and OUG 34/2014 (consumer protection, online marketplaces, withdrawal) - partial Omnibus transposition; the price-reduction rule is NOT in it (it is in HG 947/2000 art. 4^1 via HG 686/2022) -> keep (primary)
- step 56: GitHub issue search: mypy/socket/live network/JSON-LD/reference price -> large result saved; parsed below for overlap -> keep (J)

