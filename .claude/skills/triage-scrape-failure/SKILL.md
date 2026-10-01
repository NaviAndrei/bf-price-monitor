---
name: triage-scrape-failure
description: Classify why a retailer's scrape returned too little, per store, as selector drift, Cloudflare challenge, listing-readiness timeout (issue #31), true zero results, or insufficient evidence, using data/scrape_health.jsonl, data/extraction_failures.jsonl, data/scrape_health_alerts.json and run logs. Use when a scheduled run silently returns zero products, few products, or a retailer's alerts stop.
allowed-tools: Read, Grep, Glob, Bash(gh run list *), Bash(gh run view *), Bash(ls *)
---

# Triage a scrape failure

Pick exactly one outcome per retailer (eMAG, PC Garage, Flanco). Do not guess. When the
evidence does not separate two causes, the outcome is **insufficient evidence**, and you
list which extra source would separate them.

This skill reads and reports. It does not edit files, run the live scrapers, or fetch
retailer pages; the live check belongs to `/selector-healthcheck`.

## What each source really records

Only these fields are confirmed by reading `scripts/scrape.py` and the files:

- `data/scrape_health.jsonl`, one record per store per run: `store`, `run_id`,
  `run_started_utc`, `watches_requested`, `products_parsed`, `matched_count`,
  `policy_blocked_count`, `parse_failures`, `challenge_detected`,
  `challenge_wait_entered`, `latency_seconds`, `last_known_good_utc`, `fetch_attempts`,
  `rate_limit_hits`.
- `data/extraction_failures.jsonl`: `store`, `url`, `query`, `timestamp_utc`,
  `failure_reason`, `run_id`. Example reason seen: "no price found in any extraction layer".
- `data/scrape_health_alerts.json`: a list of dead-man alert strings (empty when healthy).
- **No file records issue #31 listing-readiness timeouts.** Those leave a log line,
  `WARNING [<site>] listing selector '<sel>' not attached within <ms>ms; ...`, and a
  screenshot `data/debug/<site>-<UTC-timestamp>.png`.
- The durable per-run record is the `[scrape:health]` JSON line in the job log
  (`gh run view <id> --repo NaviAndrei/bf-price-monitor --log`). Its fields are counts
  (`challenge_wait_entered`, `challenge_final_failure`, `products_parsed`,
  `parse_failures`, `watches_skipped`), not the booleans in the jsonl file. The local
  jsonl files are untracked and wiped by the runner's checkout, so they may be old or absent.

## Classes, evidence and fix path

| Outcome | Assign only when | Fix path |
|---|---|---|
| Cloudflare challenge | The store's latest record has `challenge_detected` or `challenge_wait_entered` true (or the log line shows `challenge_wait_entered` or `challenge_final_failure` above 0). `rate_limit_hits` and `fetch_attempts` above 1 support it. | `docs/runbooks/BF_WEEK_CADENCE.md` section 4 (rollback plan when a site starts blocking). Do not try to bypass; reduce cadence, wait. |
| Listing-readiness timeout (#31) | The run log shows the `WARNING [<site>] listing selector ... not attached` line for that store, or a `data/debug/<site>-*.png` file from the failing run exists. | Issue #31 stays open for live validation; check `LISTING_READY_TIMEOUT_MS` and the card selector for that site in `scripts/scrape.py`. |
| Selector drift | Page loaded (no challenge, no readiness warning) and `extraction_failures.jsonl` has recent `failure_reason` entries for the store, or `parse_failures` is high relative to `products_parsed`, **and** a live check (`/selector-healthcheck` output visible in the conversation) shows the markers missing or a stock class that `*_stock_status()` does not handle. | Run `/selector-healthcheck`, then the `add-retailer` skill's selector-update steps. |
| True zero results | Page loaded, no challenge, no readiness warning, **and** live evidence that the retailer's page shows no product cards for that query (a `/selector-healthcheck` or manual check showing the retailer's own no-results message). | Review the query in `data/watchlist.json`; no code change. |
| Insufficient evidence | None of the above is established. | List the missing source (below). |

Zero products with `parse_failures` 0, no challenge flag and no warning is ambiguous by
design: selector drift is silent, and a legitimately empty result looks the same. That
case is **insufficient evidence** until a live check separates them.

## Missing sources to name when the outcome is insufficient evidence

- The job log: `gh run list --repo NaviAndrei/bf-price-monitor --workflow monitor.yml --limit 5`
  then `gh run view <id> --repo NaviAndrei/bf-price-monitor --log`, searching for
  `listing selector`, `[scrape:health]`, `challenge`.
- `data/debug/` screenshots for the failing run (`ls data/debug`).
- A live `/selector-healthcheck` for the affected retailer.

## Output

Per retailer:

```
<store>: <outcome>
Evidence: <the exact record fields or log lines you read, with run_id and timestamp>
Next action: <fix path, or the missing source to fetch>
```

Quote only field values and log lines you actually read in this run. Never print URLs
containing tokens. `allowed-tools` only pre-approves reading commands; the read-only
behavior comes from these instructions and the repo's deny rules.
