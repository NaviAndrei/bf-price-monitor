---
name: write-tests
description: Implement pytest tests for a scripts/*.py module, mirroring the file under tests/, mocking network/LLM/Telegram calls rather than hitting real services.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Write Tests

`tests/` already holds a large suite (`tests/conftest.py` puts the repo root on
`sys.path`; `pytest.ini` sets `testpaths = tests`). Use this skill when asked to
add tests for a `scripts/*.py` module. Read the existing `tests/test_<module>.py`
first and extend it rather than starting a second file for the same module.

## Ground rules specific to this codebase

- **Never let a test hit a real network endpoint.** Every function under
  test either scrapes a live retailer, calls the Hugging Face API, or posts
  to the Telegram Bot API. Mock `requests.get`/`requests.post` (and
  `playwright.sync_api` for the Playwright-based paths) with
  `unittest.mock` or `monkeypatch` -- the retry-backoff logic was already
  verified this way in a prior session (mocked 429 responses), never against
  the real sites. This is a hard constraint, not a style preference.
- **Mirror `scripts/` under `tests/`**: `scripts/scrape.py` -> `tests/test_scrape.py`, etc.
- **Test pure logic first**: `parse_price()`, `is_challenge_page()`,
  `emag_stock_status()` / `pcgarage_stock_status()` / `flanco_stock_status()`
  (feed them a hand-built BeautifulSoup fragment, not a live fetch), and
  `load_history()`'s pre-versioning-migration branch are all pure functions
  with clear inputs/outputs -- test these before anything involving mocked
  I/O.
- **Test the retry loop's control flow**, not the real delays: monkeypatch
  `scrape.RETRY_BACKOFFS` to near-zero values in the test so it runs fast,
  and assert on the number of `requests.get` calls made and the final
  return value for a 429-then-200 sequence and a 429-exhausted sequence.
- `scripts/` has no `__init__.py`; `tests/conftest.py` already inserts the
  repo root onto `sys.path`, so `from scripts import scrape` works -- don't
  restructure `scripts/` into a package.

## Procedure

1. Read the target module fully before writing anything.
2. Identify pure functions (no I/O) vs. functions that need mocking.
3. Write `tests/test_<module>.py` with one test class or set of functions
   per source function, covering the happy path plus the edge cases already
   documented in that function's own comments (e.g. `parse_price()`'s
   Romanian `.`/`,` separator convention, `load_history()`'s legacy
   unversioned-file migration).
4. Run `python -m pytest tests/test_<module>.py -v` and show the real output
   -- do not report tests as passing without having run them.
