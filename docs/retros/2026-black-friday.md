# Black Friday 2026 retro (T-36, #47)

> **Status: template, not yet a retro.** Black Friday week (23–30 November
> 2026) has not happened. Every `TBD` below must be filled from the runner's
> real data after the event. Nothing here is evidence yet. #47 stays open
> until this document holds real numbers and the carry-over items are filed.

## 1. Generate the metrics

Run these after the last Cyber Monday run. The inputs are the three files
the monitor carries between runs (#42): `price_history.db`,
`scrape_health.jsonl` and `alert_outbox.jsonl`. The job workspace is wiped
at the start of each run, so take them from the runtime-state snapshot.
Do not run the generator inside `C:\bf-monitor-runtime-state` itself. Its
ACL is deliberately restricted, and opening SQLite, even read-only, can
create sidecar files next to the database. Instead, as an administrator on
the runner:

1. Read the snapshot id from `C:\bf-monitor-runtime-state\CURRENT`.
2. Copy the three files from `C:\bf-monitor-runtime-state\snapshots\<id>\`
   into a scratch folder, for example `data/exports/retro-input/`, which is
   git-ignored.
3. Run:

```bash
gh run list --repo NaviAndrei/bf-price-monitor --workflow monitor.yml --limit 1000 --json databaseId,conclusion,createdAt,event,status > data/exports/monitor_runs.json
uv run python scripts/generate_retro_metrics.py --db data/exports/retro-input/price_history.db --health data/exports/retro-input/scrape_health.jsonl --outbox data/exports/retro-input/alert_outbox.jsonl --runs data/exports/monitor_runs.json
```

- The default window is 2026-11-23 00:00 UTC up to 2026-12-01 00:00 UTC.
  Change it with `--start` and `--end`.
- Output goes to `data/exports/retro_metrics.json` and
  `data/exports/retro_metrics.md`. Both are git-ignored. Paste the Markdown
  summary into section 2. The JSON stays local, because it lists missed
  schedule slots and per-retailer detail.
- A metric without input data prints `no data`, never 0. If the evidence
  status is `no_event_data`, stop and find the right input files first.

### What each metric means

| Metric | Source | Definition and limits |
|---|---|---|
| Scrape runs, store runs | `scrape_health.jsonl` | Distinct `run_id`s and per-store records in the window |
| Uptime (%) | `scrape_health.jsonl` | Share of elapsed 2-hour cron slots with at least one recorded run. Needs history from before the window end, otherwise `no data` |
| Workflow success rate (%) | `gh run list` export | `success` ÷ completed `monitor.yml` runs. Scheduled runs are also reported separately |
| Deal alerts triggered, sent, dead-lettered | `alert_outbox.jsonl` | Effective status per event (the last record wins, as in `notify.py`), by `created_at_utc` |
| Delivery attempts | SQLite `delivery_attempts` | Terminal outcomes by channel and final state (#55) |
| Feedback acceptance rate (%) | SQLite `alert_feedback_current` | (useful + purchased) ÷ (useful + purchased + fake_discount + wrong_price), counting current labels. `wrong_product` and `duplicate` are counted but excluded, as in the #36 policy |
| Estimated savings | `purchased` labels plus observations | Median observed price over the prior 30 days minus the alerted price, floored at 0. Self-reported and an estimate, not a receipt |
| Extraction success rate | `scrape_health.jsonl` | products_parsed ÷ (products_parsed + parse_failures), per retailer |
| Challenge rate | `scrape_health.jsonl` | Share of a retailer's runs with `challenge_detected`. `challenge_wait_entered` is also reported |
| Failure reasons | `extraction_failures.jsonl` | Partial: this file is not carried between runner jobs, so treat it as a sample |

Retailers are ranked by extraction success rate, highest first, with ties
broken by the lower challenge rate.

## 2. Headline numbers

_Paste `data/exports/retro_metrics.md` here._

TBD

## 3. Incidents and root-cause analysis

Use one block per incident: runner downtime, Cloudflare or bot walls,
selector drift, notification outages. Sources are missed schedule slots,
zero-product runs, dead-lettered alerts, scraper-breakdown health alerts,
and the monitor.yml run logs.

### Incident TBD: _short title_

| | |
|---|---|
| Window (UTC) | TBD |
| Detected by | TBD (health alert, dead-man alert, missed slots, manual) |
| Impact | TBD (retailers affected, alerts delayed or lost, slots missed) |
| Root cause | TBD |
| Evidence | TBD (run ids, metric values; no URLs with tokens, no chat ids) |
| Fix or mitigation | TBD |
| Follow-up | TBD (issue number, or "none" with the reason) |

## 4. False-positive triage

These are alerts the raters marked `fake_discount`, `wrong_price`,
`wrong_product` or `duplicate`. List them with query A in section 7.

| Alert decision id (first 8) | Label (raters) | Rule verdict | Cause | Action |
|---|---|---|---|---|
| TBD | | | | |

Group the causes: bad reference price, extraction error, wrong product
match (#49), dedup or cooldown gap (T-13), or a genuine fake discount that
was correctly flagged. A fake discount flagged by `FAKE_DISCOUNT_SUSPECT` is
not a false positive.

## 5. False-negative analysis (missed drops)

These are large observed drops with no alert. Query B lists drops of 15% or
more. For each one, check whether an alert exists (outbox or
`delivery_attempts`), and if not, why not.

| Retailer | SKU | Drop % | When (UTC) | Why no alert | Action |
|---|---|---|---|---|---|
| TBD | | | | | |

Group the reasons: below the policy threshold, blocked by the Omnibus
30-day rule (correct), not on the watchlist, scrape missed the window,
cooldown suppression, or a notification failure.

## 6. Carry-over backlog matrix

Every row needs evidence from sections 2 to 5. When the backlog is triaged,
each row is either filed as an issue (record the number) or rejected with a
reason. The blueprint suggests two candidates. They are **not decisions**:
keep or drop them on this week's evidence.

| Item | Evidence (section) | Impact | Effort | Priority | Target | Issue |
|---|---|---|---|---|---|---|
| Candidate: Altex internal-API integration | TBD | TBD | TBD | TBD | Q1 2027 | TBD |
| Candidate: distributed proxy rotation | TBD (challenge rates) | TBD | TBD | TBD | Q1 2027 | TBD |
| TBD | | | | | | |

Open threads to re-check here:
- #36, the anomaly pilot. Evaluate it once BF labels exist (see
  docs/anomaly-pilot.md).
- #49, product identity. Re-keying `canonical_products` is still deferred.

## 7. Queries

Run these read-only, for example with `sqlite3 "file:data/price_history.db?mode=ro"`.
Bind `:start` and `:end` as ISO UTC strings, for example
`2026-11-23T00:00:00+00:00` and `2026-12-01T00:00:00+00:00`.

**A. False-positive candidates** (labels that reject the alert):

```sql
SELECT f.alert_decision_id, f.label, COUNT(*) AS raters
FROM alert_feedback_current f
WHERE f.label IN ('fake_discount', 'wrong_price', 'wrong_product', 'duplicate')
  AND f.received_at_utc >= :start AND f.received_at_utc < :end
GROUP BY f.alert_decision_id, f.label
ORDER BY raters DESC;
```

**B. Missed-drop candidates** (drops of 15% or more between consecutive
observations of the same offer):

```sql
WITH ordered AS (
  SELECT o.retailer, o.sku, p.offer_id, p.price, p.scraped_at,
         LAG(p.price) OVER (PARTITION BY p.offer_id ORDER BY p.scraped_at) AS prev_price
  FROM price_observations p JOIN offers o ON o.id = p.offer_id
)
SELECT retailer, sku, prev_price, price,
       ROUND(100.0 * (prev_price - price) / prev_price, 1) AS drop_pct, scraped_at
FROM ordered
WHERE prev_price > 0 AND price <= prev_price * 0.85
  AND scraped_at >= :start AND scraped_at < :end
ORDER BY drop_pct DESC;
```

To check whether a drop was alerted, compute its alert decision id with
`bf_price_monitor.anomaly.dataset.alert_decision_ids(url, price, retailer)`
and look it up in `delivery_attempts` or the outbox.

**C. Notification outcomes:**

```sql
SELECT channel, final_state, response_class, COUNT(*) AS attempts
FROM delivery_attempts
WHERE completed_at_utc >= :start AND completed_at_utc < :end
GROUP BY channel, final_state, response_class
ORDER BY attempts DESC;
```

## 8. Sign-off

| | |
|---|---|
| Metrics generated at (UTC) | TBD |
| Input fingerprints | TBD (from the `inputs` block of retro_metrics.json) |
| Carry-over issues filed | TBD |
| Reviewed by | TBD |
