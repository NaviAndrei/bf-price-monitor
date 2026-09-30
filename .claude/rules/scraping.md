---
paths:
  - "scripts/scrape.py"
  - "scripts/adapters/**"
---

# Scraping rules

- **Extraction hierarchy:** Always try in order: (1) JSON-LD `schema.org/Product`, (2) semantic HTML attributes, (3) CSS locators, (4) non-authoritative AI extraction fallback.
- **Resilient waits:** Never use fixed `time.sleep()` for network or selector waits in Playwright. Use auto-waiting bounded locators (`page.locator().wait_for()`).
- **Intraday observations:** Every scrape run records an `Observation` with an explicit UTC timestamp (`ISO 8601`). Never skip an observation because one already exists for the same calendar date.
- **Selector drift is silent:** a changed selector returns an empty list or `"unknown"` stock status instead of raising. After any selector edit, verify with `/selector-healthcheck` (or the `selector-drift-detector` agent) against the live page before trusting it.
