---
name: add-retailer
description: Add a new retailer scraper to scripts/scrape.py following this repo's established pattern -- live DOM inspection, price + stock_status extraction, watchlist wiring, dry-run verification.
allowed-tools: Bash, Read, Edit, Write, Glob, Grep
---

# Add a New Retailer Scraper

This repo has built the same scraper shape three times (eMAG plain
`requests`, PC Garage and Flanco via Playwright+stealth). This skill
packages that pattern so the next retailer follows the same discipline:
**never guess a selector — verify it against a real, live listing page
first.**

## Steps

1. **Confirm scope.** Read `data/watchlist.json` and `scripts/scrape.py`'s
   `SCRAPERS` dict to see which sites already exist. Confirm the new site's
   listing/search URL structure and check its `robots.txt` for any
   disallowed paths (this repo only ever scrapes listing/search pages, never
   individual product pages — keep that boundary).

2. **Fetch a real listing page and inspect the actual HTML.** Do not
   pattern-match from memory or from another site's markup. Use a short
   throwaway script (not committed) that calls `requests.get()` or, if the
   site 403s a plain request (Cloudflare or similar), `fetch_with_browser()`
   imported from `scripts/scrape.py`, then parse with BeautifulSoup and
   print out a couple of real product cards' HTML. Confirm:
   - the repeating "card" container selector
   - the title element + link (`href` must be present)
   - the price element (note the site's decimal/thousands-separator
     convention — Romanian retailers use `.` for thousands and `,` for
     decimals, same as `parse_price()` already handles)
   - **the stock-status marker** — run at least two different queries,
     including one likely to surface an out-of-stock or limited-stock item,
     since some sites (eMAG, Flanco) exclude sold-out products from search
     results entirely rather than marking them. Note every distinct
     class/attribute value you actually observe; don't assume only two states
     exist.

3. **Decide plain `requests` vs. Playwright+stealth.** If a plain
   `fetch()` call 403s or returns a Cloudflare challenge page consistently,
   use `fetch_with_browser()` instead (see the PC Garage/Flanco functions
   for the pattern) -- don't add Playwright unless the site actually needs it.

4. **Write `<site>_stock_status(card) -> str`.** Follow the existing
   functions' shape: return `"in_stock"` / `"out_of_stock"` / `"unknown"`
   (never guess a third state, never fail loudly on a missing marker --
   `"unknown"` is the deliberate safe fallback). Add a comment citing the
   date and the actual query/product count you verified against, same as
   `emag_stock_status()`, `pcgarage_stock_status()`, `flanco_stock_status()`.

5. **Write `scrape_<site>_listing(query) -> list[dict]`.** Each result dict
   needs `title`, `price`, `url`, `stock_status` (and `reference_price` if
   the site publishes a legally-mandated 30-day-low price, like Flanco
   does). Skip any card missing a title, price, or href -- same filter every
   existing scraper uses.

6. **Wire it in:**
   - Add `"<site>": scrape_<site>_listing` to the `SCRAPERS` dict.
   - Add at least one `{"site": "<site>", "query": "..."}` entry to
     `data/watchlist.json`.

7. **Dry-run before trusting it.** Run `scripts/scrape.py` once and inspect
   the console output and `data/price_history.json` for the new site's
   entries -- confirm `stock_status` values are real captured states, not
   `"unknown"` for every product (that would mean the marker selector is
   wrong, not that the site has no stock info). Never fabricate or assume a
   selector works without this run.
