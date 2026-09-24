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
New convention introduced: _derive_sku() extracts SKU from URL path segments
in scripts/migrate_history_to_sqlite.py — T-38's SKU extraction work should
either reuse or explicitly supersede this.

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