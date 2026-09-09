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

## Where bugs tend to hide in this codebase specifically

- **Encoding**: any place text scraped from a Romanian retailer (diacritics
  like "ă", "î", "ș") flows through a `print()` on Windows without
  `PYTHONIOENCODING=utf-8` will raise `UnicodeEncodeError` under `cp1252` --
  check every `print()` that includes a title or scraped string.
- **`parse_price()`**: Romanian format is `.` thousands / `,` decimal. Any
  code path that reformats or re-parses an already-parsed float (e.g.
  round-tripping through a different locale) would silently corrupt it.
  Check for any second parsing pass anywhere in the pipeline.
- **BeautifulSoup selector assumptions**: `card.select_one(...)` returns
  `None` on no match -- verify every call site checks for `None` before
  calling `.get_text()`/`.get()`/indexing on the result (a missing check
  is a live `AttributeError` risk, not a hypothetical one).
- **`HISTORY_LIMIT` slicing**: `entry["history"][-HISTORY_LIMIT:]` -- verify
  this can't silently drop the *current* day's freshly-appended entry if
  `HISTORY_LIMIT` or the append order ever changes.
- **Retry/backoff arithmetic**: `RETRY_BACKOFFS[attempt - 1]` -- verify this
  never indexes out of range across the full `range(1, MAX_FETCH_ATTEMPTS +
  1)` loop (this couples `MAX_FETCH_ATTEMPTS` to `len(RETRY_BACKOFFS)` --
  check nothing has silently decoupled them since).
- **`notify.py`'s Telegram rate limiting**: verify a burst of alerts can't
  exceed Telegram's rate limit and silently drop a message without logging
  it.
- **JSON file writes**: `json.dump(..., open(path, "w", ...))` never closes
  the file handle explicitly -- on Windows this is usually fine due to
  refcounting, but confirm no code path could leave a handle open across a
  crash between two such writes in the same run.

## Procedure

1. Read `scripts/scrape.py`, `scripts/analyze.py`, `scripts/notify.py` in
   full -- don't sample or skim.
2. For each candidate finding, construct the exact input/state that
   triggers it and trace it through the code line by line.
3. Report each finding as: file, line, the failure scenario, and the
   concrete wrong output or exception it produces. Rank most-severe first.
4. If you find nothing that survives this bar, say so plainly rather than
   padding the report with style nitpicks relabeled as bugs.
