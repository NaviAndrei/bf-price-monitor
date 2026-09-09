---
name: selector-healthcheck
description: Fetch one live listing page per retailer in watchlist.json and verify the CSS selectors scrape.py depends on still resolve, catching silent DOM drift before a scheduled run does.
disable-model-invocation: true
allowed-tools: Bash, Read, Glob
---

# Selector Health Check

This skill exists because scraping failures in this repo are **silent by
design**: a changed selector doesn't throw, it just returns an empty list or
a `stock_status` of `"unknown"` for every product. The only way to catch
drift before a scheduled run does is to periodically re-verify the real DOM
against what `scripts/scrape.py` expects.

This is user-invocable only (`disable-model-invocation: true`) — it hits
live third-party sites, so it must never run without you explicitly asking
for it via `/selector-healthcheck`.

## Read-only guarantee

`allowed-tools: Bash, Read, Glob` — this skill never edits `scripts/scrape.py`
or `data/*.json`. If a selector has drifted, it reports the mismatch; fixing
it is a separate, deliberate task (see the `add-retailer` skill for the
pattern, or edit the relevant `*_stock_status()` / `scrape_*_listing()`
function directly).

## Procedure

1. Read `data/watchlist.json` to get one `{site, query}` pair per retailer
   currently being monitored (skip `altex` — it's stubbed, see README).

2. For each site, fetch **one** live listing URL using the exact same
   URL-construction logic as `scrape.py` (do not reconstruct it from memory —
   `Read` the relevant `scrape_<site>_listing()` function first):
   - `emag`: `https://www.emag.ro/search/<query with + for spaces>`
   - `pcgarage`: `https://www.pcgarage.ro/cauta/?q=<query with + for spaces>`
   - `flanco`: `https://www.flanco.ro/catalogsearch/result/?q=<query with + for spaces>`

   Use a single polite request per site (same `HEADERS` User-Agent as
   `scrape.py`, one request — do not loop or paginate). For `pcgarage` and
   `flanco`, a plain `requests.get()` will most likely hit a Cloudflare
   challenge (they require Playwright+stealth in production); either run
   `python -c "from scripts.scrape import fetch_with_browser; ..."` for
   real coverage, or note the challenge and skip to the next site rather
   than retrying — this skill checks selectors, not bypasses blocks.

3. For each fetched page, check that these markers are present in the raw
   HTML (use `grep -c` via Bash, not a full parse) and report counts:

   | Site | Card selector | Title | Price | Stock marker |
   |------|---------------|-------|-------|---------------|
   | emag | `.card-item` | `.card-v2-title` | `.product-new-price` | `data-availability-id` attribute |
   | pcgarage | `.product_box` | `.product_box_name a` | `.product_box_price_container p.price` | `.product_box_availability` (class: `instock`/`insupplierstock`/`outofstock`) |
   | flanco | `li.product-item` | `.product-item-link` | `.price-box.price-final_price .special-price .price` | `.stocky-txt` (class: `in-stock`/`limited-stock`/`supplier-stock`/`bin-display`) |

4. Report per site: how many cards matched, how many had a resolvable
   title+price+stock marker (matching `scrape.py`'s own filter — a card with
   no title/price is silently skipped there too), and whether the stock
   marker's class/attribute values match the ones `scrape.py`'s
   `*_stock_status()` functions currently recognize. Flag any class value
   seen on the live page that isn't handled by the current function (e.g. a
   new stock-status class would fall through to `"unknown"` — that's a
   drift signal, not a crash, so it must be checked explicitly).

5. Summarize as a pass/fail table per site — do not fix anything found here;
   report it so a human decides whether to run `add-retailer`'s selector-update
   steps.
