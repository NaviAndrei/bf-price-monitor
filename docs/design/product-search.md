# Design: read-only on-demand product search

Status: **implemented as `scripts/search_product.py`** (CLI first). Written
2026-10-29. Recon covered the live code at the working tree of that date; the
external research was read the same day. `scripts/scrape.py`, `.github/`,
`data/` and `pyproject.toml` are not changed by this work.

## 1. Problem

Today a product can only be looked up by adding it to `data/watchlist.json`
and waiting for the scheduled monitor. The owner wants a manual, on-demand
command: give it a product name, it asks the repo's live shops through their
existing scrapers, and prints what it found. It must be **read-only** with
respect to the monitor: no price history, no alerts, no health logs, no
watchlist edit, no workflow or dependency change.

Deliverables of this change (three new files, nothing else is edited):

1. `docs/design/product-search.md` (this document)
2. `scripts/search_product.py`
3. `tests/test_search_product.py`

## 2. What exists (Phase A, confirmed in live code)

| Fact | Where |
|---|---|
| Registry `SCRAPERS`: emag, pcgarage, flanco, itgalaxy are real; altex is wrapped by `placeholder_scraper` and returns `[]` | `scripts/scrape.py:1256-1262`, `1223-1234` |
| Every scraper is `(query: str) -> list[dict]` and builds its own search URL from free text | `scrape.py:936, 1013, 1092, 1179` |
| Row keys: `title`, `price` (float, RON), `url`, `stock_status` (`in_stock`, `limited_stock`, `supplier_stock`, `out_of_stock`, `unknown`), `seller`, `is_marketplace`; flanco adds optional `reference_price`. **No `currency`, GTIN or MPN key** | `scrape.py:961-976, 1038-1047, 1126-1139, 1204-1215` |
| URL building: eMAG is path-based (`/search/<query with spaces as +>`), PC Garage and Flanco use `?q=<query with spaces as +>` with no percent-encoding, ITGalaxy uses `quote_plus` | `scrape.py:936, 1013, 1092, 1179` |
| `title_matches_query` is a whole-token, lowercase test with no diacritic or punctuation folding; all four scrapers call it as a bare module global, so it is looked up in `scrape` at call time | `scrape.py:757-764`; calls at `949, 1026, 1114, 1192` |
| Retry constants are read at call time: `SCRAPER_RETRY_ATTEMPTS` (2) in `with_retry`, `MAX_FETCH_ATTEMPTS` (4) in `fetch` and `fetch_with_browser`. Worst case on a 403: 4 fetches x 2 scraper attempts = 8 requests | `scrape.py:148-175, 205-207, 783, 870` |
| Signals: `fetch_attempt_counts` after every response, `rate_limit_hit_counts` for 403/429, `challenge_counts` for challenge pages, `failure_counts` for matched cards without a price | `scrape.py:353-366`, `log_extraction_failure` `677-694` |
| `RATE_LIMIT_COOLDOWN_MINUTES = 30` is the monitor's own cool-off after a block | `scrape.py:232` |
| Zero-result pages on browser shops: `_await_listing_ready` raises `ListingReadinessTimeout`, but `fetch_with_browser` catches it itself (`scrape.py:828-830`) and returns the page HTML. The scraper then parses zero cards and returns `[]`. The only traces are a screenshot in `DEBUG_DIR` and a printed warning | `scrape.py:492-516, 821-830` |
| Disk writes reachable from a scraper call: `data/extraction_failures.jsonl` (`EXTRACTION_FAILURES_FILE`, line 213) and `data/debug/<site>-<stamp>.png` (`DEBUG_DIR`, line 123). History, SQLite, health and alert files are written only by `main()` | `scrape.py:677-694, 502-516, 405-412, 1432-1438, 1919-1931` |
| `_run_state.reset(run_id)` is called only inside `_run()`; `qa_watchlist.py` never resets it (it reads counter deltas) and closes the browser in a `finally` | `scrape.py:276-285, 1599`; `qa_watchlist.py:268-282, 530-533` |
| `_pace_domain` keeps a 5 s floor per site and has no off switch; with one request per shop it never waits | `scrape.py:83-93` |
| Scrapers `print()` progress lines to stdout | throughout `scrape.py` |
| `identity._fold` (casefold + NFKD, drops combining marks and (R)/(TM) signs) and `match_titles` (accept 0.75, high 0.85) are stdlib-only | `bf_price_monitor/identity.py:34-35, 194-198, 267-309` |
| `scripts/` is outside the CI mypy and coverage targets; tests import scripts bare through `pythonpath=["scripts"]`; `.dockerignore` allows `scripts/*.py` | `pyproject.toml`; `quality.yml:71-73`; `tests/test_run_loop.py:190-205` |
| Dashboard: loopback only, token on every non-GET, git calls through an injectable fixed-argv runner | `bf_price_monitor/dashboard/server.py:205-258`, `gitcmd.py:39-55` |

The registered, non-placeholder shops are the "4 shops" used for sizing below.
Shops that stay out, per the owner's brief: evomag.ro, forit.ro, cel.ro,
altex.ro, mediagalaxy.ro, compari.ro. None except altex (a placeholder) is
registered; the script also hard-codes them as an exclusion list so a future
registration cannot pull them in by accident. The repo docs do not record the
reasons for these exclusions; this document takes them from the owner's brief.

## 3. Options

| Option | Pros | Cons |
|---|---|---|
| A. Add the product to the watchlist and run `scrape.py` | no new code | writes history, alerts and health logs; slow; not read-only |
| B. Call `scrape.SCRAPERS` from a new script, with temporary in-process overrides (**chosen**) | reuses parsing and Playwright setup; zero changes to `scrape.py`; fully offline-testable | relies on module globals staying looked up at call time (covered by a test) |
| C. Add `search_only` parameters to the scrapers | cleanest long term | changes `scrape.py`, which is out of scope |
| D. New independent scrapers | full control | duplicates the most brittle code in the repo (selector drift) |

## 4. Chosen design

Flow, strictly sequential (the Playwright sync API and the shared
`_browser_state` are not thread-safe): validate the query, pick shops, then for
each shop read the cache or make one scraper call under overrides, classify
the status, clean rows, group, print.

Shops are the `SCRAPERS` keys that are not placeholders and not excluded.
`--sites a,b` narrows them; an unknown, placeholder or excluded name gets
status `unsupported` and no request is made.

Overrides, applied together and restored in a `finally` block:

- `scrape.MAX_FETCH_ATTEMPTS = 1`, `scrape.SCRAPER_RETRY_ATTEMPTS = 1`
- `scrape.EXTRACTION_FAILURES_FILE` and `scrape.DEBUG_DIR` point into a
  `TemporaryDirectory(ignore_cleanup_errors=True)`
- `scrape.title_matches_query` is replaced by the loose matcher, unless
  `--strict` is given
- scraper stdout is redirected to stderr, so `--json` output stays clean
- `scrape._run_state.reset("search-<UTC stamp>")` gives clean counters

Cleanup order: close the browser (`scrape._browser_state.close()`), read the
failure-row count from the temporary file, restore every attribute, remove the
temporary directory.

### Output schema

```json
{
  "query": "iphone 15",
  "mode": "loose",
  "generated_at": "2026-10-29T10:00:00+00:00",
  "shops": [
    {"shop": "emag", "status": "ok", "detail": "", "offer_count": 3,
     "from_cache": false, "fetched_at": "...",
     "signals": {"challenge": 0, "rate_limit": 0, "attempts": 1}}
  ],
  "offers": [
    {"shop": "emag", "title": "...", "price": 3999.99, "currency": "RON",
     "stock_status": "in_stock", "seller": "", "is_marketplace": false,
     "url": "https://www.emag.ro/...",
     "fetched_at": "...", "match": "strict",
     "group_id": "g1", "group_confidence": "medium"}
  ],
  "groups": [{"group_id": "g1", "confidence": "medium",
              "shops": ["emag", "flanco"], "score": 0.8, "reasons": []}],
  "diagnostics": {"extraction_failure_rows": 0}
}
```

Offers are sorted with out-of-stock offers last, then by price, shop and title.
Cross-shop matching still runs on the price-only order, so the final sort does
not change which offers group together. `seller` and `is_marketplace` are copied
from the scraper row; a missing seller becomes `""` and a missing marketplace
flag becomes `false`, because ITGalaxy reports neither. The table marks
out-of-stock rows `[out]` and marketplace rows `[mkt]`. URLs pass through
`scrape.canonicalize_url`; duplicates inside one shop are dropped by canonical
URL. `currency` is the constant `RON` because scrapers return none.
`group_id` and `group_confidence` are `null` for offers without a cross-shop
match.

### Matching rules

Loose matching is the default. `--strict` switches it off and uses exactly what
the monitor uses. Loose mode is a
**superset** of strict. A row passes if the original strict matcher accepts
it, or if every query word is present in the title after: case and diacritic
folding with `identity._fold` (comma-below and cedilla s and t both fold),
decimal comma to point (`15,6` to `15.6`), splitting on non-alphanumerics (so
hyphens fold), joining a number with a following unit (`128 GB` equals
`128GB`; units `gb tb mb ghz mhz hz inch w mah`), and accepting a hyphenated
query word either as its parts or in joined form (`wi-fi` or `wifi`). Loose
mode never removes stopwords or aliases, so no query word can be dropped. A
query word with no letters or digits makes loose mode fall back to strict.
Each row is tagged `match: "strict"` if the original matcher (captured before
patching) also accepts it, otherwise `"normalized"`.

### Cross-shop grouping

Scrapers return no GTIN or MPN, so the GTIN and brand+MPN tiers from the
research are documented as future options and not built (they would need
`scrape.py` changes). Grouping uses `identity.match_titles` (accept at 0.75,
`high` at 0.85, otherwise `medium`) with complete-link comparison. Offers from
the same shop are never grouped. Hard vetoes: both titles carry capacity
tokens (`128gb`, `1tb`) and the sets differ; or only one title carries a
refurbished marker (`resigilat`, `refurbished`, `renewed`, `reconditionat`).
Groups are labelled "suggested equivalence", never authoritative.

### Query validation

Whitespace collapsed; 2 to 80 characters; at least one letter or digit;
allowed characters are **letters, digits, space and `- . ,`** only. Anything
else exits with code 2 rather than being silently cleaned, so a shop never
receives a different query than the one asked for. `/`, `+` and `'` are
excluded because eMAG's search is path-based, a raw `+` is read as a space,
and only ITGalaxy percent-encodes its query (the other three only replace
spaces with `+`). The `&`/`#` gap in those three scrapers is flagged, not fixed.

### Caching and rate limits

Without `--cache-dir` nothing is cached and nothing is written. With it, one
JSON file per `sha256(shop, folded query, mode)` is written atomically
(temp file then rename). Time-to-live by status: `ok` and `no_match` use
`--cache-ttl` (default 900 s); `blocked` uses
`scrape.RATE_LIMIT_COOLDOWN_MINUTES * 60` (read when the check runs, currently
1800 s); `error` uses 90 s. `--cache-ttl 0` stops reading `ok` and `no_match`
entries but still honours `blocked` and `error` entries inside their own
window. A cache hit also skips the pause between shops. Live calls are
spaced 5 s apart. That 5 s figure is a house convention, not a sourced limit:
the per-host pacing in `scrape._pace_domain` has a 5 s floor, but it is never
triggered here because each shop receives one request per check. `--no-delay`
removes the pause and is for local use only, matching the same flag in
`scripts/qa_watchlist.py`. When it is used without `--cache-dir`, the command
prints a warning to stderr, since repeated runs will then hit the shops again.
One request per shop is guaranteed by the overrides above.

### Failure statuses

| Status | Meaning | How it is derived |
|---|---|---|
| `ok` | at least one offer | rows left after matching |
| `blocked` | challenge, 403 or 429, reported and never retried | `challenge_counts` or `rate_limit_hit_counts` rose during the call |
| `no_match` | a page came back, nothing matched | response seen, no block signal, no rows. Also used for a browser shop whose listing never appeared (see DR-7) |
| `error` | no usable answer | scraper raised, no response at all (`fetch_attempt_counts` unchanged), or matched cards had no price (`failure_counts` rose) |
| `unsupported` | no request made | unknown, placeholder or excluded shop |

Exit code: 0 if at least one shop is `ok`, or `no_match` with any detail other
than "listing never appeared". A browser shop whose listing never appeared does
not count as answered, so a run where every shop is in that state exits 1. 2
means invalid usage, including `--sites` with no usable shop name. Known limit: a raw 5xx response or silent selector drift
is indistinguishable from "no match" because the scrapers expose no status
code or raw card count (the same limit applies to `qa_watchlist.py`).

## 5. External research (Phase B)

Labels: **sourced** means a primary source was read on 2026-10-29;
**inferred** means it follows from sourced facts or is a design judgment.

| Claim | Label | Source |
|---|---|---|
| Valid GTINs are 8, 12, 13 or 14 digits with a check digit | sourced | S1 |
| The check-digit weights (3 and 1 from the right, modulo 10) | unverified (GS1 pages blocked) | S1 gap |
| schema.org `gtin13`, `sku`, `mpn` exist; `sku` is merchant-specific | sourced | S2, S3, S4 |
| WDC Products: with 80% corner cases, word co-occurrence F1 was 52.66 (seen) and 30.57 (unseen); Ditto 74.07 and 69.49; R-SupCon 79.99 and 53.10 | sourced | S5 |
| English training pairs helped German product matching in every transformer tested | sourced | S6 |
| No Romanian product-matching dataset found | sourced (absence of a hit) | search |
| Tiered rule (GTIN, then brand+MPN, then token similarity with variant vetoes) and its thresholds | inferred | design |
| Matching 4 shops with stdlib only is proportionate | inferred | design |
| RFC 9309: a 4xx on robots.txt means access allowed, a 5xx means assume disallow; cache at most 24 h; Crawl-delay is not defined | sourced | S7 |
| Retry-After is seconds or an HTTP-date; 429 may carry it; a client should not repeat a 403 unchanged; a 304 carries validators | sourced | S8, S9 |
| Conditional requests save little on query-dependent search pages | inferred | design |
| Reporting a challenge as `blocked` instead of working around it | inferred (no standard mandates it) | S10 |
| Default TTL of 900 s, error TTL 90 s, 5 s spacing | inferred (no source) | design |
| Comma-below and cedilla s/t both decompose under NFD to a base letter plus a combining mark; stripping category `Mn` folds them; `casefold` is stronger than `lower` | sourced | S11, S12 |
| Romanian decimal separator is a comma | sourced | S13 |
| GB (10^9 bytes) differs from GiB (2^30); lowercase b is bit | sourced | S14 |
| `țol` is 25.4 mm | sourced | S15 |
| Unidecode is GPL-2.0; RapidFuzz is MIT, 3.14.6, Python 3.11+, needs the VC++ runtime on Windows | sourced | S16, S17 |

Gaps (not claimed anywhere above): GS1 check-digit pages returned 403; the
EUR-Lex text of the text-and-data-mining exception could not be read, so **no
legal claim is made** (this is not legal advice); how often Romanian shops
expose GTINs and whether they send `ETag` was not measured; Hz, W, cm and mAh
conventions in titles and Romanian stop-word lists were not sourced.

Optional dependencies (not added): **RapidFuzz** (MIT) would give faster and
better fuzzy scores; cost is one new runtime dependency in `pyproject.toml`
and `uv.lock`, a VC++ runtime on Windows, and a Docker image rebuild.
**Unidecode** is GPL-2.0 and language-blind; avoid. **ftfy** repairs mojibake
only and is not needed.

Sources (all accessed 2026-10-29):
S1 https://gs1ca.org/check-digit/ ·
S2 https://schema.org/gtin13 · S3 https://schema.org/sku · S4 https://schema.org/mpn ·
S5 https://arxiv.org/html/2301.09521v2 · S6 https://arxiv.org/abs/2110.03338 ·
S7 https://www.rfc-editor.org/rfc/rfc9309.html ·
S8 https://www.rfc-editor.org/rfc/rfc9110.html · S9 https://www.rfc-editor.org/rfc/rfc6585.html ·
S10 https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-403/ ·
S11 https://docs.python.org/3/library/unicodedata.html ·
S12 https://docs.python.org/3/library/stdtypes.html ·
S13 https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/cldr-numbers-full/main/ro/numbers.json ·
S14 https://physics.nist.gov/cuu/Units/binary.html ·
S15 https://dexonline.ro/definitie/%C8%9Bol ·
S16 https://pypi.org/project/RapidFuzz/ · S17 https://github.com/avian2/unidecode

## 6. Decision records

**DR-1: Loose matching is the default; `--strict` opts out.** The scrapers filter
titles internally and expose no switch, so a looser matcher in the new script
could never see the dropped rows. The script temporarily swaps
`scrape.title_matches_query` (looked up as a module global by all four
scrapers) and restores it in `finally`. The loose matcher is a strict superset
and never drops query words; rows are tagged `strict` or `normalized`.
Rejected: dropping loose matching (misses `casti`, `128 GB`) and changing
`scrape.py` (out of scope). Owner decision, 2026-10-29: loose matching first as
a restorable override; then made the default, with `--strict` as the opt-out
(the earlier opt-in `--loose` flag was renamed).

**DR-2: One request per shop by forcing retry constants to 1.** Today a 403
can cause up to 8 requests per shop (4 fetches times 2 scraper attempts). The
script sets `MAX_FETCH_ATTEMPTS` and `SCRAPER_RETRY_ATTEMPTS` to 1 for the
check and restores them. A 403, 429 or challenge becomes `blocked`, with no
backoff and no retry. Rejected: keeping the monitor's retries (breaks "at most
one search per shop"). Owner decision, 2026-10-29.

**DR-3: Side-effect files go to a throwaway directory.** Scrapers can append
to the extraction-failure log and save debug screenshots. The script points
`EXTRACTION_FAILURES_FILE` and `DEBUG_DIR` at a temporary directory, closes
the browser first, counts failure rows for the report, then lets the directory
go. Rejected: writing into `data/` (pollutes drift diagnostics). Owner
decision, 2026-10-29.

**DR-4: Stdlib-only matching.** For 4 shops and rows without GTIN, token
folding plus the existing `identity.match_titles` is enough. RapidFuzz is
listed as an option, not added (dependency change is out of scope).

**DR-5: Grouping reuses `identity.match_titles` plus two vetoes.** It keeps
one scoring rule across the repo. Adjacent issue, flagged not fixed:
`match_titles` always subtracts 0.1 when no CPU can be read, so phones and
SSDs top out near 0.8 and are at most `medium`.

**DR-6: Strict query allow-list.** Letters, digits, space and `- . ,` only.
`/` and `+` are out because eMAG's search is path-based and a raw `+` is read
as a space; `'` is out because three of four scrapers do not percent-encode.
Rejecting beats cleaning, so shops never get a different query than asked.

**DR-7: Status comes from counter deltas, and a missing listing is
`no_match`.** Parsing printed text is fragile. `blocked` needs a challenge or
403/429 delta; `error` needs a raised exception, no response, or lost matched
cards. A browser shop whose listing selector never attached is the normal
zero-result page, not a fault: `fetch_with_browser` swallows
`ListingReadinessTimeout` and returns HTML, leaving only a screenshot in the
temporary debug directory. The script maps that case, or a raised
`ListingReadinessTimeout`, to `no_match` with detail "listing never appeared",
provided no challenge and no 403/429 was seen. Owner amendment, 2026-10-29.
Limit found by QA on 2026-10-29: the screenshot is the only signal, and a
failed screenshot save leaves no trace, so such a page gets the detail
"no matching offers" instead. Telling the two apart fully would need a
`scrape.py` change to surface the timeout, which this task does not allow.

**DR-8: Cache keys and TTLs.** Key is `sha256(shop, folded query, mode)` so
strict and loose never share entries. `ok` and `no_match` use `--cache-ttl`
(default 900 s, a judgment call). `blocked` uses
`scrape.RATE_LIMIT_COOLDOWN_MINUTES * 60`, read at run time rather than a
literal 1800, so it follows the monitor's own cool-off if that constant
changes. `error` uses 90 s so a transient failure is retried soon without
hammering. Owner amendment, 2026-10-29.

**DR-9: Sequential execution; scraper stdout goes to stderr.** The browser
state is shared and not thread-safe. Scrapers print progress to stdout, which
would corrupt `--json`, so it is redirected.

## 7. Test plan

All tests are offline: the whole `scrape.SCRAPERS` dict is replaced, sleeps
and clocks are injected, and the three disk paths are redirected so nothing
touches `data/`. Coverage includes every status (with a fixture for the
zero-result browser page), strict and loose matching (diacritics, units,
hyphens, decimal comma, no dropped words, superset property, tags), override
restoration after success, error and interruption, browser-close ordering,
failure-row reporting, stdout isolation, sorting, grouping with vetoes, cache
TTLs per status, CLI exit codes, and a **URL capture test** that runs the four
real scrapers with the network call replaced and asserts the exact URL each
receives for a query containing a space and a dot. One regression test is added
per bug found during the build. No live request is ever made.

## 8. Rollout

1. **CLI (this change):** `.venv/Scripts/python.exe scripts/search_product.py "ssd 1tb"`.
   A live smoke run is left to the owner (at most one search per shop).
2. **Dashboard endpoint (later, not built):** `POST /api/search` (so the
   token guard applies) that runs the CLI with `--json` as a subprocess using
   the injectable-runner style of `dashboard/gitcmd.py`: fixed argv,
   `sys.executable`, repo-root working directory, validated query, hard
   timeout of about 150 s (4 shops, 5 s pauses, 25 s navigation limit each),
   no shell. New dashboard code must avoid the forbidden git-verb strings
   checked in `tests/dashboard/test_dashboard_infra.py`.

## 9. Risks

- Deep import of `identity._fold` (a private name); a public alias would be a
  small follow-up.
- Overrides mutate module globals of `scrape` for the duration of a run; safe
  in a single-purpose process, not safe if imported into the monitor.
- `no_match` can hide a 5xx or selector drift (no status code or card count is
  exposed). Selector drift is silent by design in this repo.
- Playwright shops need a Chromium install; `search_product.py` closes it in
  `finally`.
- Cloudflare-protected shops may block a headless browser; the command reports
  `blocked` and never works around it.
- Other adjacent issues, flagged and not fixed: the strict matcher ignores
  diacritics; `fetch_with_browser` returns HTML on a non-challenge 403 (the
  403 is still counted, so it shows as `blocked`); `load_history` leaks a file
  handle; eMAG, PC Garage and Flanco do not URL-encode `&` or `#`.

### Known limits

- The monitor's strict `title_matches_query` is whole-token with no punctuation
  folding. A comma attached to a query word makes it fail (confirmed
  2026-10-29: 'Casti Anker Soundcore, Wireless, Bluetooth, Dark Grey' does not
  match 'casti bluetooth'). The default loose matching recovers these listings
  and marks them with `~`; `--strict` turns that off. Fixing `scrape.py` is a
  post-Black-Friday follow-up.
- Output is UTF-8. The table and `--json` are both written as UTF-8 whatever the
  console code page, so Romanian letters survive. Any consumer, for example a
  future dashboard subprocess, must decode the output as UTF-8.

## 10. Open questions for the owner

None block this work. Defaults chosen and open to change: default cache TTL
900 s; error TTL 90 s; 5 s pause between shops; query length 2 to 80;
importing the private `_fold`; no README, `DECISIONS.md` or `progress.md`
edits (version control and handoff stay with the owner).
