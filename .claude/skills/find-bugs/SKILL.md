---
name: find-bugs
description: Scan scripts/ for concrete defects (exceptions, silent data corruption, off-by-one, unhandled edge cases) with a reproducing input for each finding -- not a style or lint pass.
allowed-tools: Read, Grep, Glob, Bash
---

# Find Bugs

Report only findings you can back with a **concrete failure scenario**:
specific input or state that causes a wrong result or an unhandled
exception. "This could theoretically be an issue" without a reproducing
case is not a finding here -- this repo already has a self-review checklist
for style/security; this skill is for actual defects.

The live history store is `data/price_history.json` (read and written by
`scrape.py`); `data/price_history.db` (SQLite) receives a dual-write of the same
observations. Line numbers below are where each point was last checked; re-find the code by
the named function, since lines drift. Items marked **unverified** are places to look, not
established problems.

## Where bugs tend to hide in this codebase specifically

- **Encoding (unverified on the runner)**: scraped Romanian text (diacritics like "ă", "î",
  "ș") flowing through `print()` on Windows raises `UnicodeEncodeError` under a `cp1252`
  console. A search of `.github/`, `scripts/*.py` and `scripts/*.ps1` found no
  `PYTHONIOENCODING` or `PYTHONUTF8` setting, so check every `print()` that includes a
  title, seller or other scraped string and whether the runner's console encoding is UTF-8.
- **`parse_price()` and the semantic parser**: `parse_price()` (`scrape.py:526`) treats `.` as
  a thousands separator and `,` as the decimal. `_parse_semantic_price_value()` (right after
  it) tries `float(raw)` **first** and only then falls back to `parse_price()`. A Romanian
  label such as `"1.299"` (meaning 1299) parses as `1.299` through the first branch.
  Trace which extraction layer produces each price, and whether any code path parses an
  already-parsed value a second time.
- **BeautifulSoup `None` results**: `card.select_one(...)` returns `None` on no match. The
  three `*_stock_status()` functions guard it (`if not el: return "unknown"`); check every
  other `select_one(...)` call site in the `scrape_*_listing()` functions before
  `.get_text()`, `.get()` or indexing.
- **History pruning**: `prune_history()` (`scrape.py:1316`) calls
  `date.fromisoformat(h["date"])` on every entry. An entry with no `"date"` key or a malformed
  date raises `KeyError`/`ValueError`, which would abort the run before `save_history()`
  (`scrape.py:1783`). Check what can produce such an entry (legacy files, hand edits). Also
  check the `min_entries` fallback (`history[-min_entries:]`) when entries are out of order.
- **Retry/backoff arithmetic**: `RETRY_BACKOFFS[attempt - 1]` is used behind
  `attempt < MAX_FETCH_ATTEMPTS` at the three retry sites (`scrape.py` around 846, 854 and
  889), and `MAX_FETCH_ATTEMPTS = 1 + len(RETRY_BACKOFFS)` (`scrape.py:194`), so the two cannot
  silently decouple. Re-check after any edit that changes either constant or adds a retry
  site (the listing-readiness retry in `fetch_with_browser()` was added later).
- **Non-atomic JSON writes**: `save_history()` (`scrape.py:412`), the alerts file
  (`scrape.py:1785`) and the health-alerts file (`scrape.py:1776`) use
  `json.dump(..., open(path, "w"))`. A crash or kill mid-write leaves a truncated file, and
  the next `load_history()` (`scrape.py:358`) then raises `json.JSONDecodeError`, so the
  scrape cannot start. Look for any code path that recovers from that, and note the handle is
  never closed explicitly.
- **Dual-write divergence (unverified)**: inside the per-product loop in `main()`, each
  observation goes to the in-memory history and to SQLite
  (`sqlite_record_observation`, around `scrape.py:1606`), while `save_history()` runs once
  after the loop. If an exception escapes after some SQLite rows were written, the two stores
  disagree. Check whether a surrounding `try`/`except` or `with_retry()` changes that.
- **Telegram rate limiting and retries**: `notify.py` sleeps 1.1 s after each send
  (`notify.py:896`, `1307`) and retries up to `MAX_ATTEMPTS = 4` with capped backoff and a
  `MAX_RETRY_AFTER_SECONDS = 60` cap (`notify.py:57-64`); unsent events live in the outbox
  and are replayed. Check that the final failed attempt leaves an outbox status and a log
  line, and that a 429 `retry_after` larger than the cap does not cause a skipped send
  without a record.

## Procedure

1. Read `scripts/scrape.py`, `scripts/analyze.py`, `scripts/notify.py` in
   full -- don't sample or skim.
2. For each candidate finding, construct the exact input/state that
   triggers it and trace it through the code line by line.
3. Report each finding as: file, line, the failure scenario, and the
   concrete wrong output or exception it produces. Rank most-severe first.
4. If you find nothing that survives this bar, say so plainly rather than
   padding the report with style nitpicks relabeled as bugs.
