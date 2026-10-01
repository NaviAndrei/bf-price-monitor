---
name: check-logic
description: Review the pipeline's business logic (price/stock recording, alert generation, genuine-vs-fake discount analysis) for correctness against this repo's own stated invariants, not general code style.
allowed-tools: Read, Grep, Glob, Bash
---

# Check Application Logic

This is a correctness review against **this repo's own stated invariants**,
not a general linting pass. Read the actual current code before asserting
anything -- the comments in `scrape.py` document specific verified behaviors
(e.g. "eMAG excludes sold-out offers from search results," "Flanco surfaces
a legally-audited 30-day-low price") that any logic change must not silently
break.

**Two stores exist.** `data/price_history.json` is the live source of truth
(`HISTORY_FILE`, `scripts/scrape.py:197`); `data/price_history.db` (SQLite,
`DB_FILE`, `scripts/scrape.py:198`) is a dual-write of the same observations
(see `docs/DECISIONS.md` on T-37). Each invariant below says which store it is about.
Line numbers are where the claim was last verified; they drift, so re-find the code by
the named function.

## Invariants to check on every review

1. **[JSON] Every scrape is its own observation.** `record_observation()`
   (`scrape.py:1298`) appends `{date, observed_at, price, stock_status}` with **no
   same-day dedup** (its comment says so); a site checked twice a day keeps both
   points. The old "one entry per product per day" rule no longer holds, so a second
   same-day entry is correct. Check instead that `date` is still written on every entry
   (consumers group by it) and that nothing re-introduces a `history[-1]["date"] == today`
   skip.
2. **[JSON] History is bounded by age, not by count.** `prune_history()`
   (`scrape.py:1316`) keeps entries within `max_days=90` of the reference date and, if
   that leaves fewer than `min_entries=2`, returns `history[-min_entries:]` so
   `prev_price` comparisons stay possible. There is no `HISTORY_LIMIT` constant any more.
   Check that `main()` still calls it after `record_observation()` (around
   `scrape.py:1573`) and that `prev_price`/`past_prices` are taken from the history
   **before** the new observation is appended (`scrape.py:1564-1566`).
3. **[JSON] Schema versioning.** `load_history()` (`scrape.py:355`) wraps a
   pre-versioning bare `{url: entry}` file into `{"schema_version": ..., "products": ...}`
   without dropping a product, then canonicalizes keys (`_canonicalize_product_keys`,
   `scrape.py:366`); `save_history()` (`scrape.py:412`) always writes the versioned
   shape with `HISTORY_SCHEMA_VERSION` (`scrape.py:203`, currently 1).
4. **[JSON + SQLite] Dual-write stays in step.** In `main()`, each observation goes to
   the history dict and to `sqlite_record_observation(...)` (around `scrape.py:1606-1625`)
   with the same `observed_at` and the same `in_stock = stock_status != "out_of_stock"`.
   JSON stays authoritative; SQLite insertion must be idempotent on a repeated call
   (`tests/unit/test_sqlite_storage.py::test_record_observation_duplicate_call_is_idempotent`).
5. **[parsing] Stock-status vocabulary.** Each `<site>_stock_status()`
   (`emag` `scrape.py:914`, `pcgarage` `scrape.py:988`, `flanco` `scrape.py:1059`) must
   return one of exactly five strings: `"in_stock"`, `"limited_stock"`,
   `"supplier_stock"`, `"out_of_stock"`, `"unknown"`. Never `None`, never a raw class
   string, never raise on a missing element (each returns `"unknown"` when its marker is
   absent). Flanco's `bin-display` class maps to `"in_stock"`.
6. **[fetch] Retry vs. hard-block.** `RETRY_STATUS_CODES = (403, 429)` and
   `MAX_FETCH_ATTEMPTS = 1 + len(RETRY_BACKOFFS)` (`scrape.py:192-194`). In
   `fetch_with_browser()` (`scrape.py:782`) two things trigger a re-attempt of the whole
   fetch, both bounded by `MAX_FETCH_ATTEMPTS` and both indexing
   `RETRY_BACKOFFS[attempt - 1]`: a retryable status, and a listing-readiness timeout
   (issue #31). Neither may retry on other status codes. The bounded
   Cloudflare clear-wait inside one attempt (`_wait_out_challenge`, `scrape.py:467`) is a
   different mechanism and must stay separate from the retry loop.
7. **[alerts] Alert gate.** `should_alert()` (`scrape.py:1219`) returns False when
   `prev_price is None` (a first sighting never alerts), when `new_price >= prev_price`
   (increases and unchanged prices never alert), or when `stock_status == "out_of_stock"`.
   Otherwise it applies the micro-drop floor (`drop_val >= 5.0` RON **and**
   `drop_percent >= 2.0`), the all-time-low override according to `atl_policy`
   (`aggressive`, `conservative`, `off`; anything else raises), `target_price`, and
   `min_drop_percent`. Check that a change keeps that order and the first-sighting rule.
8. **[analysis] Deterministic primacy.** `evaluate_omnibus_rule()`
   (`analyze.py:113`) decides `rule_verdict` from alert data alone;
   `enforce_deterministic_invariant()` (`analyze.py:418`) overwrites `verdict` and
   `is_recommended` from it on every `get_analysis()` path; `main()` merges
   `{**alert_fields, **metrics, **analysis, ...}`, so the analysis dict must not gain a key
   that exists in `metrics`. For a diff touching these, use the
   `verdict-integrity-reviewer` agent as well.
9. **[analysis] What the prompt contains.** `build_omnibus_prompt()` (`analyze.py:221`)
   interpolates title, site, seller, new and old price, the 30-day-low line (labelled by
   `thirty_day_window_provenance`), reference price, history days and the computed verdict.
   It does **not** include `stock_status`; the old claim that it must is gone.
   Out-of-stock items are kept away from analysis by `should_alert()` (invariant 7).
   Do not "restore" stock status to the prompt without a reason.

## Procedure

1. Read `scripts/scrape.py`, `scripts/analyze.py`, `scripts/notify.py` in
   full.
2. Check each invariant above against the current code, quoting the exact
   line(s) that satisfy or violate it, and say which store (JSON or SQLite) it concerns.
3. Report violations with the concrete input that triggers them (e.g. "a
   product added for the first time today would satisfy `prev_price is
   None`, so no alert fires -- confirmed correct" or "X would break if Y").
4. Do not propose stylistic changes or refactors here -- this skill is
   scoped to correctness against stated invariants, not code quality.
5. If an invariant above no longer matches the code, report that as a finding and
   update this file; do not silently skip it.
