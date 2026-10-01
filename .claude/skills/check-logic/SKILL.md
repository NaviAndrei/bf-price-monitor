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

## Invariants to check on every review

1. **`data/price_history.json` idempotency**: `main()` in `scrape.py` must
   never append two history entries for the same product on the same date
   (`if entry["history"] and entry["history"][-1]["date"] == today: continue`)
   and must never let `entry["history"]` exceed `HISTORY_LIMIT` (30 entries).
2. **Schema versioning**: `load_history()` must correctly migrate a
   pre-versioning bare `{url: entry}` file into `{"schema_version": 1,
   "products": {...}}` without dropping any existing product, and
   `save_history()` must always write the versioned shape.
3. **Stock-status fallback safety**: every `<site>_stock_status()` function
   must return one of exactly `"in_stock"` / `"out_of_stock"` / `"unknown"`
   -- never `None`, never a raw class string, never raise on a missing
   element.
4. **Retry vs. hard-block distinction**: the `RETRY_STATUS_CODES` (403, 429)
   retry loop in `fetch()`/`fetch_with_browser()` must not retry on other
   status codes, must not exceed `MAX_FETCH_ATTEMPTS`, and must leave the
   inner Cloudflare `is_challenge_page()` polling loop inside
   `fetch_with_browser()` completely untouched -- these are two different
   mechanisms for two different failure modes and must not be merged.
5. **Alert generation**: an alert in `alerts.json` should only be emitted
   when `prev_price is not None and prev_price != r["price"]` -- check that
   a brand-new product (no prior history) never generates a spurious
   "price change" alert on its first sighting.
6. **`analyze.py`'s prompt context**: `build_prompt()` must include
   `stock_status` so the genuine-vs-fake verdict can be conditioned on it --
   confirm this wasn't dropped in a later edit.

## Procedure

1. Read `scripts/scrape.py`, `scripts/analyze.py`, `scripts/notify.py` in
   full.
2. Check each invariant above against the current code, quoting the exact
   line(s) that satisfy or violate it.
3. Report violations with the concrete input that triggers them (e.g. "a
   product added for the first time today would satisfy `prev_price is
   None`, so no alert fires -- confirmed correct" or "X would break if Y").
4. Do not propose stylistic changes or refactors here -- this skill is
   scoped to correctness against stated invariants, not code quality.
