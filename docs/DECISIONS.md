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